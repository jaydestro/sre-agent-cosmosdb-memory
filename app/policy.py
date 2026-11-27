"""Fail-closed policy evaluation; model output never grants authority."""

from datetime import datetime, timedelta

from app.simulation import utcnow


def default_policy(settings):
    return {
        "id": "payments-pool-policy",
        "version": 1,
        "enabled": True,
        "scope_id": settings.scope_id,
        "service_id": settings.service_id,
        "allowed_operations": ["pool_adjust"],
        "from_value": 50,
        "to_value": 100,
        "expires_at": (utcnow() + timedelta(days=1)).isoformat(),
        "approval_expiration_seconds": min(settings.approval_expiration_seconds, 300),
        "max_attempts": 3,
        "max_actions": 10,
        "cooldown_seconds": 30,
        "max_concurrency": 1,
        "allow_rollback": False,
        "rollback_from_value": 100,
        "rollback_to_value": 50,
    }


def parse_time(value):
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("UTC offset required")
    return parsed


def validate_diagnostic_proof(proof, proposal, snapshot, max_age_seconds, now=None):
    """Require all four current tool observations, not a model's confidence flag."""
    now = now or utcnow()
    try:
        if not isinstance(proof, dict) or (
            type(proof["target_version"]) is not int
            or type(proof["proposed_value"]) is not int
            or proof["target_version"] != proposal["target_version"]
            or proof["target_version"] != snapshot["version"]
            or proof["proposed_value"] != proposal["proposed_value"]
            or proposal["operation"] != "pool_adjust"
        ):
            return False
        observed = proof["proof"]
        fields = {
            "health": ("healthy", "error_rate", "latency_ms"),
            "config": ("pool_size", "config_valid"),
            "backend": ("backend_capacity", "demand"),
            "metrics": ("metrics", "demand", "pool_size", "backend_capacity"),
        }
        for name, expected_fields in fields.items():
            result = observed[name]
            age = (now - parse_time(result["observed_at"])).total_seconds()
            if (
                result["source"] != "current_simulation_telemetry"
                or result["historical"] is not False
                or type(result["version"]) is not int
                or result["version"] != snapshot["version"]
                or not -1 <= age <= max_age_seconds
                or any(result[key] != snapshot[key] for key in expected_fields)
            ):
                return False
        return (
            snapshot["root_cause"] == "pool_exhaustion"
            and snapshot["config_valid"] is True
            and snapshot["healthy"] is False
            and snapshot["pool_size"]
            < snapshot["demand"]
            <= proposal["proposed_value"]
            <= min(100, snapshot["backend_capacity"])
        )
    except (KeyError, TypeError, ValueError, OverflowError):
        return False


def evaluate_policy(policy, proposal, snapshot, control, now=None):
    now = now or utcnow()

    def deny(reason):
        return {"allowed": False, "decision": "deny", "reason": reason}

    try:
        if control["mode"] != "autonomous" or control.get("suspended"):
            return deny("Autonomous execution is disabled")
        if not policy["enabled"] or parse_time(policy["expires_at"]) <= now:
            return deny("Policy disabled or expired")
        if any(
            proposal[k] != policy[k] or snapshot[k] != policy[k] for k in ("scope_id", "service_id")
        ):
            return deny("Policy scope or service mismatch")
        if proposal["operation"] != "pool_adjust" or policy["allowed_operations"] != [
            "pool_adjust"
        ]:
            return deny("Operation is not narrowly authorized")
        if policy["from_value"] != 50 or policy["to_value"] != 100:
            return deny("Policy exceeds safe bounds")
        for key, low, high in (
            ("max_attempts", 1, 3),
            ("max_actions", 1, 10),
            ("cooldown_seconds", 1, 3600),
            ("approval_expiration_seconds", 1, 300),
        ):
            if type(policy[key]) is not int or not low <= policy[key] <= high:
                return deny("Policy exceeds budget bounds")
        rollback = bool(proposal.get("rollback_of"))
        if rollback:
            if (
                policy.get("allow_rollback") is not True
                or policy.get("rollback_from_value") != 100
                or policy.get("rollback_to_value") != 50
                or proposal["current_value"] != 100
                or proposal["proposed_value"] != 50
            ):
                return deny("Rollback is not explicitly narrowly authorized")
        elif proposal["current_value"] != 50 or proposal["proposed_value"] != 100:
            return deny("Only the bounded 50 to 100 adjustment is authorized")
        if (
            snapshot["pool_size"] != proposal["current_value"]
            or snapshot["version"] != proposal["target_version"]
        ):
            return deny("Target precondition changed")
        if not rollback and (
            not snapshot["config_valid"]
            or snapshot["root_cause"] != "pool_exhaustion"
            or not 50 < snapshot["demand"] <= 100
            or snapshot["backend_capacity"] < 100
            or snapshot["backend_capacity"] < snapshot["demand"]
        ):
            return deny("Fresh telemetry contradicts safe pool correction")
        if rollback and (
            snapshot["healthy"]
            or snapshot["backend_capacity"] < 100
            or not 0 < snapshot["demand"] <= 100
        ):
            return deny("Fresh telemetry contradicts narrow rollback")
        if parse_time(proposal["expires_at"]) <= now:
            return deny("Proposal expired")
        if control.get("attempts", 0) >= policy["max_attempts"]:
            return deny("Attempt budget exhausted")
        if control.get("actions", 0) >= policy["max_actions"]:
            return deny("Action budget exhausted")
        if control.get("inflight") not in (None, proposal["id"]):
            return deny("Concurrent remediation is active")
        if policy["max_concurrency"] != 1:
            return deny("Unsupported concurrency")
        last = control.get("last_action_at")
        if last and (now - parse_time(last)).total_seconds() < policy["cooldown_seconds"]:
            return deny("Cooldown active")
    except (KeyError, TypeError, ValueError, OverflowError):
        return deny("Malformed policy or evidence")
    return {"allowed": True, "decision": "allow", "reason": "Bounded pool exhaustion correction"}
