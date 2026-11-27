"""Deterministic, persisted development-only payments workload."""

from datetime import datetime, timezone

from app.errors import Conflict, Forbidden, Invalid, Unavailable
from app.security import authorize


def utcnow():
    return datetime.now(timezone.utc)


def timestamp():
    return utcnow().isoformat()


def metrics(state):
    pool, demand, capacity = (state[k] for k in ("pool_size", "demand", "backend_capacity"))
    if not state["config_valid"]:
        cause, error, latency = "deployment_error", 0.8, 1800
    elif demand > capacity:
        cause, error, latency = "backend_overload", (demand - capacity) / demand, 1400
    elif demand > pool:
        cause, error, latency = "pool_exhaustion", (demand - pool) / demand, 1200
    else:
        cause, error, latency = "healthy", 0.0, 80
    relief = state.get("restart_relief_until")
    if relief and utcnow() < datetime.fromisoformat(relief) and cause != "healthy":
        latency = max(100, latency // 2)
    return {
        "latency_ms": latency,
        "error_rate": round(error, 4),
        "pool_utilization": round(min(demand / pool, 1.0), 4),
        "root_cause": cause,
        "healthy": cause == "healthy",
    }


class Simulation:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings
        self.state_id = f"service:{settings.service_id}"

    def initialize(self):
        if self.settings.scope_id != "demo-payments-development":
            raise Forbidden("Simulation seed is restricted to development")
        if self.repo.read(self.state_id):
            return
        try:
            self.repo.create(
                {
                    "id": self.state_id,
                    "scope_id": self.settings.scope_id,
                    "type": "service",
                    "service_id": self.settings.service_id,
                    "pool_size": 50,
                    "demand": 90,
                    "backend_capacity": 150,
                    "config_valid": True,
                    "version": 1,
                    "restart_count": 0,
                    "last_remediation": None,
                }
            )
        except Conflict:
            if not self.repo.read(self.state_id):
                raise

    def snapshot(self):
        state = self.repo.read(self.state_id)
        if state is None:
            raise Unavailable("Run the explicit development seed first")
        if (
            state.get("scope_id") != self.settings.scope_id
            or state.get("service_id") != self.settings.service_id
            or state.get("type") != "service"
        ):
            raise Forbidden("Target scope mismatch")
        if (
            any(
                type(state.get(k)) is not int or state[k] <= 0
                for k in ("pool_size", "backend_capacity", "version")
            )
            or type(state.get("demand")) is not int
            or state["demand"] < 0
            or type(state.get("config_valid")) is not bool
            or type(state.get("restart_count")) is not int
            or state["restart_count"] < 0
        ):
            raise Invalid("Persisted simulation state is invalid")
        derived = metrics(state)
        return {**state, "metrics": derived, **derived, "observed_at": timestamp()}

    def inject(self, scenario, principal):
        if (
            self.settings.scope_id != "demo-payments-development"
            or not self.settings.allow_demo_reset
        ):
            raise Forbidden("Development reset is disabled")
        if principal.kind != "human":
            raise Forbidden("Human operator required")
        authorize(principal, "operator", self.settings.scope_id)
        if scenario not in {"pool_exhaustion", "deployment_error", "healthy"}:
            raise Invalid("Unknown scenario")
        state = self.snapshot()
        values = {
            "pool_size": 50 if scenario == "pool_exhaustion" else 100,
            "demand": 90,
            "backend_capacity": 150,
            "config_valid": scenario != "deployment_error",
            "version": state["version"] + 1,
            "last_remediation": None,
            "restart_relief_until": None,
        }
        self.repo.patch(
            self.state_id,
            [{"op": "set", "path": f"/{k}", "value": v} for k, v in values.items()],
            state["_etag"],
        )
        return self.snapshot()
