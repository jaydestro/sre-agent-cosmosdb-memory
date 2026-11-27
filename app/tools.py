"""Read-only, fixed-target diagnostic functions exposed to Agent Framework."""

import asyncio
import time

from app.errors import Invalid, Unavailable
from app.security import authorize
from app.simulation import Simulation


class DiagnosticTools:
    allowed_names = (
        "health",
        "logs",
        "metrics",
        "config",
        "backend",
        "deployment",
        "history",
        "evidence",
    )

    def __init__(self, repo, settings, principal, incident_id, evidence=None):
        self.repo, self.settings, self.principal = repo, settings, principal
        self.incident_id, self.evidence_service = incident_id, evidence
        self.simulation = Simulation(repo, settings)
        self.activity = []
        self.started = time.monotonic()
        self.results = {}
        self.calls = 0

    def _begin(self, name):
        authorize(self.principal, "viewer", self.settings.scope_id)
        if self.calls >= self.settings.agent_max_tool_calls:
            raise Unavailable("Diagnostic tool-call budget exhausted.")
        if time.monotonic() - self.started >= self.settings.agent_timeout_seconds:
            raise Unavailable("Diagnostic deadline exceeded.")
        self.calls += 1
        return time.perf_counter()

    async def _snapshot(self, name, fields):
        start = self._begin(name)
        snapshot = await asyncio.to_thread(self.simulation.snapshot)
        result = {key: snapshot.get(key) for key in fields}
        result.update(
            {
                "source": "current_simulation_telemetry",
                "historical": False,
                "observed_at": snapshot["observed_at"],
                "version": snapshot["version"],
            }
        )
        self.results[name] = result
        self.activity.append(
            {
                "tool": name,
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "source": result["source"],
                "status": "ok",
            }
        )
        return result

    async def health(self) -> dict:
        """Read current payments health and error rate; does not mutate the service."""
        return await self._snapshot("health", ["healthy", "error_rate", "latency_ms"])

    async def logs(self, limit: int = 10) -> dict:
        """Read bounded persisted incident events for this incident only."""
        start = self._begin("logs")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise Invalid("Log result limit must be between 1 and 20.")
        events = await asyncio.to_thread(
            self.repo.query, "audit", filters={"incident_id": self.incident_id}, limit=limit
        )
        result = {
            "events": [
                {k: v for k, v in event.items() if not k.startswith("_")} for event in events
            ],
            "source": "persisted_incident_audit",
            "historical": True,
        }
        self.results["logs"] = result
        self.activity.append(
            {
                "tool": "logs",
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "status": "ok",
            }
        )
        return result

    async def metrics(self) -> dict:
        """Read current demand, utilization, error rate, and latency."""
        return await self._snapshot(
            "metrics", ["metrics", "demand", "pool_size", "backend_capacity"]
        )

    async def config(self) -> dict:
        """Read current pool configuration and configuration validity."""
        return await self._snapshot("config", ["pool_size", "config_valid", "version"])

    async def backend(self) -> dict:
        """Read current backend capacity and demand before considering a pool change."""
        return await self._snapshot("backend", ["backend_capacity", "demand"])

    async def deployment(self) -> dict:
        """Read current deployment configuration health and restart count."""
        return await self._snapshot("deployment", ["config_valid", "restart_count"])

    async def history(self, limit: int = 5) -> dict:
        """Read bounded persisted resolutions for this fixed service."""
        start = self._begin("history")
        if type(limit) is not int or not 1 <= limit <= 20:
            raise Invalid("History result limit must be between 1 and 20.")
        records = await asyncio.to_thread(
            self.repo.query,
            "resolution",
            filters={"service_id": self.settings.service_id},
            limit=limit,
        )
        result = {
            "resolutions": [
                {
                    "id": r["id"],
                    "incident_id": r.get("incident_id"),
                    "operation": r.get("operation"),
                    "status": r.get("status"),
                    "verification": r.get("verification"),
                }
                for r in records
            ],
            "historical": True,
            "authority": False,
        }
        self.results["history"] = result
        self.activity.append(
            {
                "tool": "history",
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "status": "ok",
            }
        )
        return result

    async def evidence(self, query: str) -> dict:
        """Retrieve bounded historical artifacts using native Cosmos vector search."""
        start = self._begin("evidence")
        if self.evidence_service is None:
            raise Unavailable("Evidence service is unavailable.")
        result = await self.evidence_service.search(query, self.principal)
        self.results["evidence"] = result
        self.activity.append(
            {
                "tool": "evidence",
                "duration_ms": round((time.perf_counter() - start) * 1000, 2),
                "ru": result["ru"],
                "status": "ok",
            }
        )
        return result

    def native_functions(self, priority=()):
        names = self.allowed_names
        ordered = [name for name in priority if name in names]
        ordered += [name for name in names if name not in ordered]
        return [getattr(self, name) for name in ordered]

    def verified_pool_proposal(self, value):
        if type(value) is not int or not 1 <= value <= 100:
            return None
        health = self.results.get("health")
        config = self.results.get("config")
        backend = self.results.get("backend")
        metric = self.results.get("metrics")
        if health is None or config is None or backend is None or metric is None:
            return None
        required = (health, config, backend, metric)
        if len({result["version"] for result in required}) != 1:
            return None
        if (
            health["healthy"]
            or not config["config_valid"]
            or metric["demand"] <= config["pool_size"]
            or metric["demand"] > backend["backend_capacity"]
            or not metric["demand"] <= value <= backend["backend_capacity"]
        ):
            return None
        return {
            "proposed_value": value,
            "target_version": config["version"],
            "proof": {"health": health, "config": config, "backend": backend, "metrics": metric},
        }
