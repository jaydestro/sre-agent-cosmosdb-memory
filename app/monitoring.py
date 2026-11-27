"""One bounded unattended worker; every outcome is durably observable."""

import asyncio
import hashlib
import logging
from datetime import timedelta
from uuid import uuid4

from app.errors import Conflict, Unavailable
from app.policy import parse_time, validate_diagnostic_proof
from app.remediation import TARGET_FIELDS, content_digest, guard, replacement
from app.security import Principal
from app.simulation import timestamp, utcnow

LOG = logging.getLogger(__name__)


class Monitor:
    def __init__(self, repo, settings, governance, investigator, memory=None):
        self.repo, self.settings = repo, settings
        self.governance, self.investigator, self.memory = governance, investigator, memory
        self._stop = asyncio.Event()
        self._lock = asyncio.Lock()
        self.principal = Principal(
            "payments-monitor",
            frozenset({"operator"}),
            frozenset({settings.scope_id}),
            kind="service",
        )

    async def run_once(self):
        if self._lock.locked():
            return {"status": "busy"}
        async with self._lock:
            results = await asyncio.to_thread(self.governance.execute_pending)
            await self._record_memory()
            control = await asyncio.to_thread(self.governance.control)
            if control["mode"] != "autonomous" or control.get("suspended"):
                return {"status": control["mode"], "results": results}
            snapshot = await asyncio.to_thread(self.governance.simulation.snapshot)
            investigation_control_version = control["version"]
            investigation_policy_digest = content_digest(control["policy"])
            if snapshot["healthy"]:
                return {"status": "healthy", "results": results}
            key = f"{self.settings.scope_id}:{snapshot['version']}:{snapshot['root_cause']}"
            incident_id = "incident:" + hashlib.sha256(key.encode()).hexdigest()[:32]
            incident = await asyncio.to_thread(self.repo.read, incident_id)
            lease = (
                utcnow() + timedelta(seconds=self.settings.agent_timeout_seconds + 5)
            ).isoformat()
            lease_token = uuid4().hex
            if incident:
                if (
                    incident["status"] != "investigating"
                    or parse_time(incident["lease_expires_at"]) > utcnow()
                ):
                    return {
                        "status": incident["status"],
                        "incident_id": incident_id,
                        "results": results,
                    }
                claim = replacement(
                    incident,
                    version=incident["version"] + 1,
                    lease_expires_at=lease,
                    lease_token=lease_token,
                    recovered_at=timestamp(),
                )
            else:
                incident = self.governance.document(
                    "incident",
                    incident_id,
                    version=1,
                    status="investigating",
                    target_version=snapshot["version"],
                    root_cause=snapshot["root_cause"],
                    thread_id=incident_id,
                    created_at=timestamp(),
                    lease_expires_at=lease,
                    lease_token=lease_token,
                )
                claim = ("create", (incident,))
            try:
                await asyncio.to_thread(self.repo.batch, [guard(control), guard(snapshot), claim])
            except Conflict:
                return {"status": "deduplicated", "incident_id": incident_id, "results": results}
            try:
                if not self.settings.allow_azure_use:
                    raise Unavailable("Azure investigation disabled; model fallback prohibited")
                analysis = await asyncio.wait_for(
                    self.investigator.analyze(
                        incident_id,
                        self.principal,
                        question="Investigate payment health with fresh evidence.",
                    ),
                    timeout=self.settings.agent_timeout_seconds,
                )
                if not isinstance(analysis, dict):
                    raise Unavailable("Investigator returned invalid structured output")
                if analysis.get("error") or analysis.get("status") in {"failed", "escalated"}:
                    raise Unavailable("Investigator did not complete successfully")
                owned_incident = await asyncio.to_thread(
                    self.governance._read, incident_id, "incident"
                )
                if (
                    owned_incident.get("lease_token") != lease_token
                    or owned_incident["status"] != "investigating"
                ):
                    raise Conflict("Investigation lease was superseded")
                control = await asyncio.to_thread(self.governance.control)
                if (
                    control["mode"] != "autonomous"
                    or control.get("suspended")
                    or control["version"] != investigation_control_version
                    or content_digest(control["policy"]) != investigation_policy_digest
                ):
                    raise Conflict("Autonomous mode was stopped during investigation")
                suggested = analysis.get("proposal")
                if (
                    analysis.get("current_proof") is not True
                    or analysis.get("conclusion_status") != "supported_by_current_tools"
                    or not isinstance(suggested, dict)
                    or not suggested.get("id")
                ):
                    raise Unavailable("Investigation lacks a supported current-tool proposal")
                proposal = await asyncio.to_thread(
                    self.governance._read, suggested["id"], "remediation"
                )
                fresh = await asyncio.to_thread(self.governance.simulation.snapshot)
                proof = analysis.get("diagnostic_proof")
                if (
                    proposal["incident_id"] != incident_id
                    or proposal["target_state"] != {k: snapshot[k] for k in TARGET_FIELDS}
                    or fresh["version"] != snapshot["version"]
                    or suggested.get("target_version") != proposal["target_version"]
                    or suggested.get("digest") != proposal["digest"]
                    or proof != proposal.get("diagnostic_proof")
                    or not validate_diagnostic_proof(
                        proof, proposal, fresh, self.settings.agent_timeout_seconds
                    )
                ):
                    raise Conflict("Investigator diagnostic proof or target binding changed")
                decision = await asyncio.to_thread(self.governance.authorize_policy, proposal["id"])
                return {
                    "status": decision["status"],
                    "incident_id": incident_id,
                    "results": results,
                }
            except asyncio.CancelledError:
                await asyncio.to_thread(
                    self._incident_error, incident_id, "Investigation cancelled", lease_token
                )
                raise
            except Exception as exc:
                await asyncio.to_thread(
                    self._incident_error, incident_id, type(exc).__name__, lease_token
                )
                return {
                    "status": "escalated",
                    "incident_id": incident_id,
                    "error": type(exc).__name__,
                    "results": results,
                }

    def _incident_error(self, incident_id, error, lease_token=None):
        incident = self.governance._read(incident_id, "incident")
        if lease_token is not None and incident.get("lease_token") != lease_token:
            return
        control = self.governance._persist_control()
        failures = control.get("model_failures", 0) + 1
        try:
            self.repo.batch(
                [
                    replacement(incident, status="escalated", error=error, updated_at=timestamp()),
                    replacement(
                        control,
                        model_failures=failures,
                        suspended=control.get("suspended", False) or failures >= 3,
                    ),
                    (
                        "create",
                        (
                            self.governance._audit(
                                f"{incident_id}:investigation_failed",
                                "investigation_failed",
                                incident_id=incident_id,
                                error=error,
                            ),
                        ),
                    ),
                ]
            )
        except Conflict:
            # The incident's winner is authoritative; never overwrite its newer state.
            return

    async def _record_memory(self):
        if self.memory is None:
            return
        events = await asyncio.to_thread(
            self.repo.query, "memory_outbox", filters={"status": "pending"}, limit=10
        )
        for event in events:
            result = await asyncio.to_thread(
                self.governance._read, event["resolution_id"], "resolution"
            )
            if result["status"] != "verified" or not result["verified"]:
                raise Conflict("Unverified resolution cannot enter procedural memory")
            try:
                confirmation = await asyncio.wait_for(
                    self.memory.record_verified(result), timeout=self.settings.agent_timeout_seconds
                )
                if not isinstance(confirmation, dict) or confirmation.get("status") not in {
                    "confirmed",
                    "done",
                    "complete",
                }:
                    raise Unavailable("Memory extraction did not confirm durable completion")
                await asyncio.to_thread(
                    self.repo.batch,
                    [guard(result), replacement(event, status="done", completed_at=timestamp())],
                )
            except asyncio.CancelledError:
                raise
            except Conflict:
                continue
            except Exception as exc:
                try:
                    attempts = event.get("attempts", 0) + 1
                    await asyncio.to_thread(
                        self.repo.batch,
                        [
                            replacement(
                                event,
                                attempts=attempts,
                                status=("escalated" if attempts >= 3 else "pending"),
                                error=type(exc).__name__,
                                updated_at=timestamp(),
                            )
                        ],
                    )
                except Conflict:
                    continue

    async def run(self):
        self._stop.clear()
        while not self._stop.is_set():
            try:
                await self.run_once()
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                try:
                    await asyncio.to_thread(
                        self.repo.create,
                        self.governance._audit(
                            f"monitor:{timestamp()}", "monitor_error", error=type(exc).__name__
                        ),
                    )
                except Exception as audit_error:
                    LOG.warning(
                        "Monitor error audit could not be persisted (%s); retrying next iteration.",
                        type(audit_error).__name__,
                    )
            try:
                await asyncio.wait_for(
                    self._stop.wait(), timeout=self.settings.monitor_interval_seconds
                )
            except asyncio.TimeoutError:
                pass

    def stop(self):
        self._stop.set()
