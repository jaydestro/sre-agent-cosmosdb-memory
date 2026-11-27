"""Transactional authorization, execution, recovery, and verification."""

import hashlib
import hmac
import json
import math
from datetime import timedelta
from uuid import uuid4

from app.errors import Conflict, Forbidden, Invalid
from app.policy import default_policy, evaluate_policy, parse_time, validate_diagnostic_proof
from app.security import authorize
from app.simulation import Simulation, timestamp, utcnow

BOUND_FIELDS = (
    "id",
    "scope_id",
    "service_id",
    "incident_id",
    "operation",
    "current_value",
    "proposed_value",
    "target_version",
    "control_version",
    "policy_id",
    "policy_version",
    "created_at",
    "expires_at",
    "evidence",
    "rollback_of",
    "thread_id",
    "proposer",
    "risk",
    "expected_impact",
    "rollback",
    "target_state",
    "policy_digest",
    "diagnostic_proof",
)

TARGET_FIELDS = (
    "version",
    "pool_size",
    "demand",
    "backend_capacity",
    "config_valid",
    "restart_count",
    "last_remediation",
)


def content_digest(value):
    def canonical(item):
        # Cosmos normalizes integral JSON floats to integers on persistence.
        if type(item) is float and math.isfinite(item) and item.is_integer():
            return int(item)
        if isinstance(item, dict):
            return {key: canonical(nested) for key, nested in item.items()}
        if isinstance(item, (list, tuple)):
            return [canonical(nested) for nested in item]
        return item

    return hashlib.sha256(
        json.dumps(
            canonical(value), sort_keys=True, separators=(",", ":"), allow_nan=False
        ).encode()
    ).hexdigest()


def proposal_digest(proposal):
    payload = {k: proposal.get(k) for k in BOUND_FIELDS}
    return content_digest(payload)


def replacement(doc, **values):
    body = {k: v for k, v in doc.items() if not k.startswith("_")}
    body.update(values)
    return ("replace", (doc["id"], body), {"if_match_etag": doc["_etag"]})


def guard(doc):
    field = "version" if "version" in doc else "status"
    return (
        "patch",
        (doc["id"], [{"op": "set", "path": f"/{field}", "value": doc[field]}]),
        {"if_match_etag": doc["_etag"]},
    )


class Governance:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings
        self.simulation = Simulation(repo, settings)

    def document(self, kind, identifier, **values):
        return {
            "id": identifier,
            "scope_id": self.settings.scope_id,
            "type": kind,
            "service_id": self.settings.service_id,
            **values,
        }

    def control(self):
        value = self.repo.read("control")
        if value:
            self._scope(value)
            return value
        return self.document(
            "control",
            "control",
            mode="guarded",
            version=1,
            policy=default_policy(self.settings),
            attempts=0,
            actions=0,
            inflight=None,
            suspended=False,
            last_action_at=None,
        )

    def _persist_control(self):
        control = self.control()
        if "_etag" not in control:
            try:
                self.repo.create(control)
            except Conflict:
                pass
            control = self.control()
        return control

    def _scope(self, doc):
        if (
            doc.get("scope_id") != self.settings.scope_id
            or doc.get("service_id") != self.settings.service_id
        ):
            raise Forbidden("Document scope or target mismatch")

    def _human(self, principal, role="operator"):
        if principal.kind != "human":
            raise Forbidden("Human authentication required")
        authorize(principal, role, self.settings.scope_id)

    def _read(self, identifier, kind):
        doc = self.repo.read(identifier)
        if not doc or doc.get("type") != kind:
            raise Invalid("Document not found")
        self._scope(doc)
        return doc

    def set_mode(self, mode, principal, expected_version, policy=None):
        self._human(principal, "admin")
        if mode not in {"guarded", "autonomous", "paused"}:
            raise Invalid("Unknown operating mode")
        control = self._persist_control()
        if type(expected_version) is not int or expected_version != control["version"]:
            raise Conflict("Control version changed")
        next_policy = control["policy"]
        if policy is not None:
            if not isinstance(policy, dict):
                raise Invalid("Policy must be structured")
            next_policy = {**policy, "version": control["policy"]["version"] + 1}
            self._validate_policy(next_policy)
        updated = {
            **control,
            "mode": mode,
            "version": control["version"] + 1,
            "policy": next_policy,
        }
        self.repo.batch(
            [
                replacement(control, **{k: v for k, v in updated.items() if not k.startswith("_")}),
                (
                    "create",
                    (
                        self._audit(
                            f"mode:{updated['version']}",
                            "mode_changed",
                            subject=principal.subject,
                            mode=mode,
                        ),
                    ),
                ),
            ]
        )
        return self.control()

    def _validate_policy(self, policy):
        baseline = default_policy(self.settings)
        try:
            for key in (
                "id",
                "scope_id",
                "service_id",
                "allowed_operations",
                "from_value",
                "to_value",
                "max_concurrency",
                "rollback_from_value",
                "rollback_to_value",
            ):
                if policy[key] != baseline[key]:
                    raise Invalid("Policy exceeds supported bounds")
            for key, low, high in (
                ("max_attempts", 1, 3),
                ("max_actions", 1, 10),
                ("cooldown_seconds", 1, 3600),
                ("approval_expiration_seconds", 1, 300),
            ):
                if type(policy[key]) is not int or not low <= policy[key] <= high:
                    raise Invalid("Policy budget exceeds supported bounds")
            if (
                type(policy["enabled"]) is not bool
                or type(policy["allow_rollback"]) is not bool
                or parse_time(policy["expires_at"]) <= utcnow()
            ):
                raise Invalid("Policy must have a future expiration")
        except (KeyError, TypeError, ValueError):
            raise Invalid("Malformed policy") from None

    def _audit(self, identifier, event, **values):
        return self.document(
            "audit", f"audit:{identifier}", event=event, observed_at=timestamp(), **values
        )

    def propose(
        self, incident_id, proposed_value, principal, evidence=None, *, operation="pool_adjust"
    ):
        authorize(principal, "operator", self.settings.scope_id)
        if principal.kind not in {"human", "service"}:
            raise Forbidden("Unsupported principal")
        if principal.kind == "service" and (
            principal.subject != "payments-monitor" or principal.roles != frozenset({"operator"})
        ):
            raise Forbidden("Only the narrow monitoring service may propose")
        if not isinstance(incident_id, str) or not incident_id or len(incident_id) > 160:
            raise Invalid("Incident identifier required")
        if operation not in {"pool_adjust", "restart", "restore_config"}:
            raise Invalid("Unsupported operation")
        if type(proposed_value) is not int:
            raise Invalid("Integer bounded value required")
        if (
            operation == "pool_adjust"
            and not 1 <= proposed_value <= 100
            or operation in {"restart", "restore_config"}
            and proposed_value != 1
        ):
            raise Invalid("Unsafe operation value")
        control = self._persist_control()
        if control["mode"] == "paused" or control.get("suspended"):
            raise Conflict("Remediation is paused or suspended")
        state = self.simulation.snapshot()
        diagnostic_proof = None
        if isinstance(evidence, list) and len(evidence) == 1 and isinstance(evidence[0], dict):
            candidate = evidence[0]
            if candidate.get("source") == "current_diagnostic_tools":
                observations = {
                    k: candidate.get(k) for k in ("health", "config", "backend", "metrics")
                }
                config_observation = observations["config"]
                diagnostic_proof = {
                    "target_version": config_observation.get("version")
                    if isinstance(config_observation, dict)
                    else None,
                    "proposed_value": proposed_value,
                    "proof": observations,
                }
                if not validate_diagnostic_proof(
                    diagnostic_proof,
                    {
                        "target_version": state["version"],
                        "proposed_value": proposed_value,
                        "operation": operation,
                    },
                    state,
                    self.settings.agent_timeout_seconds,
                ):
                    raise Invalid("Current diagnostic proof is incomplete, stale, or contradictory")
        if principal.kind == "service" and diagnostic_proof is None:
            raise Invalid("Monitoring proposals require complete current diagnostic proof")
        incident = self.repo.read(incident_id)
        if incident:
            self._scope(incident)
        else:
            incident = self.repo.create(
                self.document(
                    "incident",
                    incident_id,
                    status="detected",
                    version=1,
                    root_cause=state["root_cause"],
                    target_version=state["version"],
                )
            )
        current = (
            state["pool_size"]
            if operation == "pool_adjust"
            else state["restart_count"]
            if operation == "restart"
            else int(state["config_valid"])
        )
        now = utcnow()
        expiration = min(
            self.settings.approval_expiration_seconds,
            control["policy"].get("approval_expiration_seconds", 300),
            300,
        )
        proposal = self.document(
            "remediation",
            f"remediation:{uuid4().hex}",
            incident_id=incident_id,
            thread_id=incident.get("thread_id", incident_id),
            status="proposed",
            operation=operation,
            current_value=current,
            proposed_value=proposed_value,
            target_version=state["version"],
            control_version=control["version"],
            policy_id=control["policy"]["id"],
            policy_version=control["policy"]["version"],
            policy_digest=content_digest(control["policy"]),
            target_state={k: state[k] for k in TARGET_FIELDS},
            diagnostic_proof=diagnostic_proof,
            created_at=now.isoformat(),
            expires_at=(now + timedelta(seconds=expiration)).isoformat(),
            evidence=[
                {
                    "source": "persisted_simulation",
                    "state": {
                        k: state[k]
                        for k in (
                            "version",
                            "pool_size",
                            "demand",
                            "backend_capacity",
                            "config_valid",
                            "root_cause",
                            "metrics",
                            "observed_at",
                        )
                    },
                }
            ],
            rollback_of=None,
            proposer=principal.subject,
            risk="bounded development simulation",
            expected_impact="Restore healthy pool capacity"
            if operation == "pool_adjust"
            else "Temporary relief only"
            if operation == "restart"
            else "Restore valid configuration",
            rollback={"requires_new_authorization": True, "value": current},
        )
        proposal["digest"] = proposal_digest(proposal)
        self.repo.batch(
            [
                guard(control),
                guard(state),
                guard(incident),
                ("create", (proposal,)),
                (
                    "create",
                    (
                        self._audit(
                            proposal["id"],
                            "proposed",
                            proposal_id=proposal["id"],
                            subject=principal.subject,
                        ),
                    ),
                ),
            ]
        )
        return self._read(proposal["id"], "remediation")

    def _integrity(self, proposal, digest=None):
        expected = proposal_digest(proposal)
        if not hmac.compare_digest(str(proposal.get("digest", "")), expected):
            raise Conflict("Proposal content changed")
        if digest is not None and not hmac.compare_digest(str(digest), expected):
            raise Conflict("Approval digest does not match")

    def approve(self, proposal_id, digest, principal):
        self._human(principal)
        return self._queue(proposal_id, "human", principal.subject, digest)

    def reject(self, proposal_id, digest, principal):
        self._human(principal)
        proposal = self._read(proposal_id, "remediation")
        self._integrity(proposal, digest)
        if proposal["status"] != "proposed":
            raise Conflict("Proposal is no longer pending")
        control = self._persist_control()
        self.repo.batch(
            [
                guard(control),
                replacement(proposal, status="rejected"),
                (
                    "create",
                    (
                        self._audit(
                            f"{proposal_id}:rejected",
                            "rejected",
                            proposal_id=proposal_id,
                            subject=principal.subject,
                        ),
                    ),
                ),
            ]
        )
        return self._read(proposal_id, "remediation")

    def authorize_policy(self, proposal_id):
        return self._queue(proposal_id, "policy", "monitor", None)

    def _queue(self, proposal_id, source, subject, digest):
        proposal = self._read(proposal_id, "remediation")
        self._integrity(proposal, digest)
        if proposal["status"] == "queued":
            authorization = self._read(f"authorization:{proposal_id}", "authorization")
            if authorization["source"] == source and authorization["subject"] == subject:
                return proposal
            raise Conflict("Proposal already authorized")
        if proposal["status"] != "proposed":
            raise Conflict("Proposal is no longer pending")
        control, state = self._persist_control(), self.simulation.snapshot()
        try:
            reason = self._preconditions(proposal, control, state)
        except (Conflict, KeyError, TypeError, ValueError):
            reason = "Proposal or policy integrity failed"
        if reason:
            return self._escalate(proposal, control, None, reason)
        if source == "policy":
            decision = evaluate_policy(control["policy"], proposal, state, control)
            if not decision["allowed"]:
                return self._escalate(proposal, control, None, decision["reason"])
        elif control["mode"] != "guarded":
            raise Conflict("Human approval requires guarded mode")
        authorization = self.document(
            "authorization",
            f"authorization:{proposal_id}",
            proposal_id=proposal_id,
            status="active",
            source=source,
            subject=subject,
            digest=proposal["digest"],
            principal_kind="human" if source == "human" else "service",
            control_version=control["version"],
            policy_version=control["policy"]["version"],
            policy_id=control["policy"]["id"],
            expires_at=proposal["expires_at"],
        )
        outbox = self.document(
            "outbox",
            f"outbox:{proposal_id}",
            proposal_id=proposal_id,
            incident_id=proposal["incident_id"],
            status="pending",
            version=1,
        )
        self.repo.batch(
            [
                guard(state),
                replacement(control, inflight=proposal_id),
                replacement(proposal, status="queued"),
                ("create", (authorization,)),
                ("create", (outbox,)),
                (
                    "create",
                    (
                        self._audit(
                            f"{proposal_id}:authorized",
                            "authorized",
                            proposal_id=proposal_id,
                            source=source,
                            subject=subject,
                            digest=proposal["digest"],
                        ),
                    ),
                ),
            ]
        )
        return self._read(proposal_id, "remediation")

    def _preconditions(self, proposal, control, state):
        self._integrity(proposal)
        if control["mode"] == "paused" or control.get("suspended"):
            return "Emergency pause or suspension"
        if control["version"] != proposal["control_version"]:
            return "Control version changed"
        if (
            control["policy"]["id"] != proposal["policy_id"]
            or control["policy"]["version"] != proposal["policy_version"]
            or content_digest(control["policy"]) != proposal["policy_digest"]
        ):
            return "Policy version changed"
        if parse_time(proposal["expires_at"]) <= utcnow():
            return "Proposal expired"
        if state["version"] != proposal["target_version"]:
            return "Target version changed"
        if {k: state[k] for k in TARGET_FIELDS} != proposal["target_state"]:
            return "Target evidence changed"
        proof = proposal.get("diagnostic_proof")
        if proof is not None and not validate_diagnostic_proof(
            proof, proposal, state, self.settings.agent_timeout_seconds
        ):
            return "Current diagnostic proof is stale or contradictory"
        if (
            proposal.get("proposer") == "payments-monitor"
            and proof is None
            and not proposal.get("rollback_of")
        ):
            return "Monitoring proposal lacks current diagnostic proof"
        policy = control["policy"]
        if not policy.get("enabled") or parse_time(policy["expires_at"]) <= utcnow():
            return "Policy disabled or expired"
        if control.get("inflight") not in (None, proposal["id"]):
            return "Concurrent remediation active"
        if control.get("attempts", 0) >= policy["max_attempts"]:
            return "Attempt budget exhausted"
        if control.get("actions", 0) >= policy["max_actions"]:
            return "Action budget exhausted"
        last = control.get("last_action_at")
        if last and (utcnow() - parse_time(last)).total_seconds() < policy["cooldown_seconds"]:
            return "Cooldown active"
        operation, value = proposal["operation"], proposal["proposed_value"]
        if proposal.get("rollback_of"):
            original = self._read(proposal["rollback_of"], "remediation")
            resolution = self._read(f"resolution:{original['id']}", "resolution")
            if (
                original["status"] != "failed"
                or resolution["status"] != "failed"
                or not resolution["verification"]["applied"]
                or proposal["current_value"] != original["proposed_value"]
                or value != original["current_value"]
                or original["applied_version"] != state["version"]
            ):
                return "Rollback is not bound to a failed persisted mutation"
        if operation == "pool_adjust":
            if state["pool_size"] != proposal["current_value"] or type(value) is not int:
                return "Pool precondition changed"
            if not 1 <= value <= min(100, state["backend_capacity"]):
                return "Unsafe pool value"
            if not proposal.get("rollback_of") and (
                not state["config_valid"]
                or state["root_cause"] != "pool_exhaustion"
                or value < state["demand"]
                or state["demand"] > state["backend_capacity"]
            ):
                return "Contradictory fresh pool telemetry"
        elif operation == "restart":
            if value != 1 or state["restart_count"] != proposal["current_value"]:
                return "Restart precondition changed"
        elif operation == "restore_config":
            if value != 1 or state["config_valid"]:
                return "Configuration restore precondition changed"
        else:
            return "Unsupported operation"
        return None

    def _escalate(self, proposal, control, outbox, reason, authorization=None):
        ops = [
            replacement(proposal, status="escalated", error=reason),
            replacement(
                control,
                inflight=None
                if control.get("inflight") == proposal["id"]
                else control.get("inflight"),
            ),
        ]
        if outbox:
            ops.append(replacement(outbox, status="escalated", error=reason))
        if authorization:
            ops.append(replacement(authorization, status="revoked"))
        incident = self._read(proposal["incident_id"], "incident")
        ops.extend(
            [
                replacement(incident, status="escalated", error=reason),
                (
                    "create",
                    (
                        self._audit(
                            f"{proposal['id']}:escalated",
                            "escalated",
                            proposal_id=proposal["id"],
                            reason=reason,
                        ),
                    ),
                ),
            ]
        )
        self.repo.batch(ops)
        return self._read(proposal["id"], "remediation")

    def execute_pending(self):
        results = []
        for event in self.repo.query("outbox", filters={"status": "pending"}, limit=20):
            try:
                result = self._execute(event["id"])
                if result:
                    results.append(result)
            except Conflict:
                # A competing worker or administrative change won. Retry from persisted state.
                continue
        for event in self.repo.query("outbox", filters={"status": "verifying"}, limit=20):
            try:
                result = self._verify(event["id"])
                if result:
                    results.append(result)
            except Conflict:
                continue
        return results

    def _execute(self, event_id):
        event = self._read(event_id, "outbox")
        if event["status"] != "pending":
            return None
        proposal = self._read(event["proposal_id"], "remediation")
        authorization = self._read(f"authorization:{proposal['id']}", "authorization")
        control, state = self._persist_control(), self.simulation.snapshot()
        try:
            reason = self._preconditions(proposal, control, state)
        except (Conflict, KeyError, TypeError, ValueError):
            reason = "Proposal or policy integrity failed"
        if event["incident_id"] != proposal["incident_id"]:
            reason = "Outbox incident binding changed"
        if proposal["status"] != "queued":
            reason = "Proposal is not queued"
        if (
            authorization["status"] != "active"
            or authorization["digest"] != proposal["digest"]
            or authorization["proposal_id"] != proposal["id"]
            or authorization["control_version"] != control["version"]
            or authorization["policy_version"] != control["policy"]["version"]
            or authorization["policy_id"] != control["policy"]["id"]
            or authorization["expires_at"] != proposal["expires_at"]
            or parse_time(authorization["expires_at"]) <= utcnow()
        ):
            reason = "Authorization expired, consumed, or changed"
        if authorization["source"] == "policy":
            decision = evaluate_policy(control["policy"], proposal, state, control)
            if not decision["allowed"]:
                reason = decision["reason"]
        elif (
            authorization["source"] != "human"
            or control["mode"] != "guarded"
            or authorization.get("principal_kind") != "human"
        ):
            reason = "Authorization mode mismatch"
        if reason:
            return self._escalate(proposal, control, event, reason, authorization)
        changes = {"version": state["version"] + 1, "last_remediation": proposal["id"]}
        if proposal["operation"] == "pool_adjust":
            changes["pool_size"] = proposal["proposed_value"]
        elif proposal["operation"] == "restore_config":
            changes["config_valid"] = True
        else:
            changes.update(
                restart_count=state["restart_count"] + 1,
                restart_relief_until=(utcnow() + timedelta(seconds=20)).isoformat(),
            )
        incident = self._read(proposal["incident_id"], "incident")
        before = {k: v for k, v in state.items() if not k.startswith("_")}
        self.repo.batch(
            [
                replacement(state, **changes),
                replacement(
                    control,
                    actions=control.get("actions", 0) + 1,
                    attempts=control.get("attempts", 0) + 1,
                    last_action_at=timestamp(),
                ),
                replacement(authorization, status="consumed", consumed_at=timestamp()),
                replacement(
                    proposal, status="executing", before=before, applied_version=changes["version"]
                ),
                replacement(event, status="verifying"),
                replacement(incident, status="executing"),
                (
                    "create",
                    (
                        self._audit(
                            f"{proposal['id']}:applied",
                            "applied",
                            proposal_id=proposal["id"],
                            digest=proposal["digest"],
                        ),
                    ),
                ),
            ]
        )
        return self._verify(event_id)

    def _verify(self, event_id):
        event = self._read(event_id, "outbox")
        if event["status"] != "verifying":
            return None
        proposal = self._read(event["proposal_id"], "remediation")
        state = self.simulation.snapshot()
        control = self._persist_control()
        authorization = self._read(f"authorization:{proposal['id']}", "authorization")
        incident = self._read(proposal["incident_id"], "incident")
        self._integrity(proposal)
        applied = (
            state.get("last_remediation") == proposal["id"]
            and state["version"] == proposal["applied_version"]
            and authorization["status"] == "consumed"
        )
        rolled_back = (
            applied
            and bool(proposal.get("rollback_of"))
            and state["pool_size"] == proposal["proposed_value"]
        )
        verified = applied and state["healthy"] and not proposal.get("rollback_of")
        status = "rolled_back" if rolled_back else "verified" if verified else "failed"
        result = self.document(
            "resolution",
            f"resolution:{proposal['id']}",
            proposal_id=proposal["id"],
            incident_id=proposal["incident_id"],
            thread_id=proposal["thread_id"],
            status=status,
            verified=verified,
            before=proposal["before"],
            after=state,
            verification={
                "healthy": state["healthy"],
                "applied": applied,
                "observed_at": state["observed_at"],
            },
            created_at=timestamp(),
            operation=proposal["operation"],
        )
        failures = control.get("failures", 0) + (0 if verified or rolled_back else 1)
        ops = [
            guard(state),
            guard(authorization),
            replacement(
                control,
                inflight=None,
                failures=failures,
                suspended=control.get("suspended", False) or failures >= 3,
            ),
            replacement(proposal, status=status, resolution_id=result["id"]),
            replacement(event, status="done" if verified or rolled_back else "failed"),
            replacement(incident, status="escalated" if rolled_back or failures >= 3 else status),
            ("create", (result,)),
            (
                "create",
                (self._audit(f"{proposal['id']}:verified", status, proposal_id=proposal["id"]),),
            ),
        ]
        if verified:
            ops.append(
                (
                    "create",
                    (
                        self.document(
                            "memory_outbox",
                            f"memory:{proposal['id']}",
                            status="pending",
                            proposal_id=proposal["id"],
                            resolution_id=result["id"],
                            attempts=0,
                        ),
                    ),
                )
            )
        elif applied and proposal["operation"] == "pool_adjust" and not proposal.get("rollback_of"):
            now = utcnow()
            rollback = {k: v for k, v in proposal.items() if not k.startswith("_")}
            rollback.update(
                id=f"remediation:rollback:{proposal['id']}",
                status="proposed",
                operation="pool_adjust",
                current_value=state["pool_size"],
                proposed_value=proposal["current_value"],
                rollback_of=proposal["id"],
                target_version=state["version"],
                control_version=control["version"],
                policy_id=control["policy"]["id"],
                policy_version=control["policy"]["version"],
                policy_digest=content_digest(control["policy"]),
                target_state={k: state[k] for k in TARGET_FIELDS},
                diagnostic_proof=None,
                created_at=now.isoformat(),
                expires_at=(now + timedelta(seconds=300)).isoformat(),
                evidence=[{"source": "failed_persisted_verification", "state": state}],
                risk="Restore prior bounded pool state; escalate unresolved health",
                expected_impact="Restore prior configuration; escalate",
                rollback={"requires_new_authorization": True, "value": state["pool_size"]},
            )
            for key in ("before", "applied_version", "resolution_id"):
                rollback.pop(key, None)
            rollback["digest"] = proposal_digest(rollback)
            ops.append(("create", (rollback,)))
        self.repo.batch(ops)
        return self._read(result["id"], "resolution")
