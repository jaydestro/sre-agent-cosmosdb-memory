import asyncio
import hashlib
import json
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.agent import AgentService
from app.errors import Unavailable
from app.monitoring import Monitor
from app.remediation import Governance
from app.security import Principal
from app.simulation import utcnow
from app.tools import DiagnosticTools


class Investigator:
    def __init__(self, governance, callback=None):
        self.governance = governance
        self.calls = []
        self.callback = callback

    async def analyze(self, incident_id, principal, question=""):
        self.calls.append((incident_id, principal, question))
        if self.callback:
            return await self.callback(incident_id, principal)
        tools = DiagnosticTools(
            self.governance.repo, self.governance.settings, principal, incident_id
        )
        for function in (tools.health, tools.config, tools.backend, tools.metrics):
            await function()
        proof = tools.verified_pool_proposal(100)
        proposal = None
        if proof:
            proposal = await asyncio.to_thread(
                self.governance.propose,
                incident_id,
                100,
                principal,
                evidence=[{"source": "current_diagnostic_tools", **proof["proof"]}],
            )
        return {
            "summary": "Current diagnostic result",
            "proposal": proposal,
            "diagnostic_proof": proof,
            "current_proof": proof is not None,
            "conclusion_status": (
                "supported_by_current_tools" if proof else "unverified_model_hypothesis"
            ),
        }


class Memory:
    def __init__(self):
        self.calls = []
        self.fail = True

    async def record_verified(self, result):
        self.calls.append(result)
        if self.fail:
            raise RuntimeError("transient memory failure")
        return {"status": "done"}


@pytest.fixture
def setup(repo, settings):
    settings.allow_azure_use = True
    governance = Governance(repo, settings)
    governance.simulation.initialize()
    admin = Principal("admin", frozenset({"admin"}), frozenset({settings.scope_id}))
    governance.set_mode("autonomous", admin, 1)
    return governance, admin


@pytest.mark.asyncio
async def test_unattended_monitor_detects_deduplicates_and_verifies(repo, settings, setup):
    governance, _ = setup
    investigator = Investigator(governance)
    monitor = Monitor(repo, settings, governance, investigator)
    first = await monitor.run_once()
    assert first["status"] == "queued"
    assert investigator.calls[0][1].kind == "service"
    assert investigator.calls[0][1].roles == frozenset({"operator"})
    assert len(repo.query("incident")) == 1
    second = await monitor.run_once()
    assert second["status"] == "healthy"
    assert second["results"][0]["verified"]
    await monitor.run_once()
    assert len(investigator.calls) == 1
    assert len(repo.query("resolution")) == 1


@pytest.mark.asyncio
async def test_disabled_azure_escalates_without_invoking_model(repo, settings, setup):
    governance, _ = setup
    settings.allow_azure_use = False
    investigator = Investigator(governance)
    monitor = Monitor(repo, settings, governance, investigator)
    assert (await monitor.run_once())["status"] == "escalated"
    assert investigator.calls == []
    assert repo.query("incident")[0]["error"] == "Unavailable"
    assert repo.query("audit", filters={"incident_id": repo.query("incident")[0]["id"]})


@pytest.mark.asyncio
async def test_model_failure_is_durable_and_not_fake_success(repo, settings, setup):
    governance, _ = setup

    async def fail(*_):
        raise RuntimeError("secret model endpoint should not be persisted")

    investigator = Investigator(governance, fail)
    monitor = Monitor(repo, settings, governance, investigator)
    assert (await monitor.run_once())["status"] == "escalated"
    assert (await monitor.run_once())["status"] == "escalated"
    assert len(investigator.calls) == 1
    assert repo.query("incident")[0]["error"] == "RuntimeError"
    assert not repo.query("remediation")
    assert governance.simulation.snapshot()["pool_size"] == 50


@pytest.mark.asyncio
async def test_pause_during_investigation_prevents_new_authorization(repo, settings, setup):
    governance, admin = setup

    async def pause(*_):
        governance.set_mode("paused", admin, governance.control()["version"])
        return {"summary": "Pool exhaustion"}

    monitor = Monitor(repo, settings, governance, Investigator(governance, pause))
    assert (await monitor.run_once())["status"] == "escalated"
    assert not repo.query("outbox")
    assert governance.simulation.snapshot()["pool_size"] == 50


@pytest.mark.asyncio
async def test_deployment_error_does_not_generate_pool_correction(repo, settings, setup):
    governance, _ = setup
    state = governance.simulation.snapshot()
    repo.patch(
        state["id"], [{"op": "set", "path": "/config_valid", "value": False}], state["_etag"]
    )
    monitor = Monitor(repo, settings, governance, Investigator(governance))
    assert (await monitor.run_once())["status"] == "escalated"
    assert not repo.query("authorization")
    assert governance.simulation.snapshot()["pool_size"] == 50


@pytest.mark.asyncio
async def test_memory_outbox_retries_only_verified_resolution(repo, settings, setup):
    governance, _ = setup
    memory = Memory()
    monitor = Monitor(repo, settings, governance, Investigator(governance), memory)
    await monitor.run_once()
    await monitor.run_once()
    pending = repo.query("memory_outbox", filters={"status": "pending"})
    assert len(pending) == 1 and pending[0]["attempts"] == 1
    assert memory.calls[0]["status"] == "verified"
    memory.fail = False
    await monitor.run_once()
    assert repo.query("memory_outbox")[0]["status"] == "done"
    assert len(memory.calls) == 2


@pytest.mark.asyncio
async def test_one_bounded_worker_and_stop(repo, settings, setup):
    governance, _ = setup
    entered = asyncio.Event()
    release = asyncio.Event()

    async def wait(*_):
        entered.set()
        await release.wait()
        return {"summary": "Pool exhaustion"}

    monitor = Monitor(repo, settings, governance, Investigator(governance, wait))
    task = asyncio.create_task(monitor.run_once())
    await entered.wait()
    assert (await monitor.run_once())["status"] == "busy"
    release.set()
    await task
    run = asyncio.create_task(monitor.run())
    await asyncio.sleep(0)
    monitor.stop()
    await asyncio.wait_for(run, timeout=1)


@pytest.mark.asyncio
async def test_same_monitor_task_recovers_when_query_and_audit_both_fail(
    repo, settings, setup, monkeypatch, caplog
):
    governance, _ = setup
    monitor = Monitor(repo, settings, governance, Investigator(governance))
    assert (await monitor.run_once())["status"] == "queued"
    proposal = repo.query("remediation")[0]
    assert repo.query("outbox")[0]["status"] == "pending"
    settings.monitor_interval_seconds = 0.01
    loop = asyncio.get_running_loop()
    audit_failed, verified = asyncio.Event(), asyncio.Event()
    original_query, original_create, original_batch = repo.query, repo.create, repo.batch

    def unavailable_query(*args, **kwargs):
        raise Unavailable("Cosmos outage")

    def unavailable_audit(*args, **kwargs):
        loop.call_soon_threadsafe(audit_failed.set)
        raise Unavailable("Cosmos audit unavailable")

    def observe_batch(*args, **kwargs):
        result = original_batch(*args, **kwargs)
        if original_query("resolution", filters={"proposal_id": proposal["id"]}):
            loop.call_soon_threadsafe(verified.set)
        return result

    monkeypatch.setattr(repo, "query", unavailable_query)
    monkeypatch.setattr(repo, "create", unavailable_audit)
    monkeypatch.setattr(repo, "batch", observe_batch)
    task = asyncio.create_task(monitor.run())
    try:
        await asyncio.wait_for(audit_failed.wait(), timeout=1)
        monkeypatch.setattr(repo, "query", original_query)
        monkeypatch.setattr(repo, "create", original_create)
        await asyncio.wait_for(verified.wait(), timeout=1)
        assert not task.done()
        assert repo.read(proposal["id"])["status"] == "verified"
        assert repo.query("outbox")[0]["status"] == "done"
        assert governance.simulation.snapshot()["healthy"]
        assert "audit could not be persisted" in caplog.text
        assert not [entry for entry in repo.query("audit") if entry["event"] == "monitor_error"]
    finally:
        monitor.stop()
        await asyncio.wait_for(task, timeout=1)


@pytest.mark.asyncio
async def test_timeout_is_durable_escalation(repo, settings, setup):
    governance, _ = setup
    settings.agent_timeout_seconds = 0.01

    async def never(*_):
        await asyncio.sleep(60)

    monitor = Monitor(repo, settings, governance, Investigator(governance, never))
    result = await monitor.run_once()
    assert result["status"] == "escalated"
    assert repo.query("incident")[0]["error"] == "TimeoutError"


@pytest.mark.asyncio
async def test_repeated_distinct_model_failures_suspend(repo, settings, setup):
    governance, _ = setup

    async def fail(*_):
        raise RuntimeError()

    monitor = Monitor(repo, settings, governance, Investigator(governance, fail))
    for index in range(3):
        state = governance.simulation.snapshot()
        repo.patch(
            state["id"], [{"op": "set", "path": "/version", "value": index + 2}], state["_etag"]
        )
        await monitor.run_once()
    assert governance.control()["suspended"]
    assert governance.control()["model_failures"] == 3


@pytest.mark.asyncio
async def test_paused_worker_recovers_outbox_without_new_work(repo, settings, setup):
    governance, admin = setup
    value = governance.propose("incident:pause", 100, admin)
    governance.authorize_policy(value["id"])
    governance.set_mode("paused", admin, governance.control()["version"])
    investigator = Investigator(governance)
    monitor = Monitor(repo, settings, governance, investigator)
    assert (await monitor.run_once())["status"] == "paused"
    assert investigator.calls == []
    assert repo.read(value["id"])["status"] == "escalated"
    assert governance.simulation.snapshot()["pool_size"] == 50


@pytest.mark.asyncio
async def test_pending_memory_extraction_is_not_confirmed(repo, settings, setup):
    governance, _ = setup

    class PendingMemory:
        async def record_verified(self, result):
            return {"status": "pending"}

    monitor = Monitor(repo, settings, governance, Investigator(governance), PendingMemory())
    await monitor.run_once()
    await monitor.run_once()
    assert repo.query("memory_outbox")[0]["status"] == "pending"
    await monitor.run_once()
    await monitor.run_once()
    assert repo.query("memory_outbox")[0]["status"] == "escalated"


@pytest.mark.asyncio
async def test_expired_investigation_lease_recovers_fresh_session(repo, settings, setup):
    governance, _ = setup
    investigator = Investigator(governance)
    monitor = Monitor(repo, settings, governance, investigator)
    # Persist an incident as if a process died after claiming, before model execution.
    snapshot = governance.simulation.snapshot()
    key = f"{settings.scope_id}:{snapshot['version']}:{snapshot['root_cause']}"
    incident_id = "incident:" + hashlib.sha256(key.encode()).hexdigest()[:32]
    repo.create(
        governance.document(
            "incident",
            incident_id,
            version=1,
            status="investigating",
            thread_id=incident_id,
            root_cause=snapshot["root_cause"],
            target_version=snapshot["version"],
            lease_expires_at=(utcnow() - timedelta(seconds=1)).isoformat(),
        )
    )
    result = await monitor.run_once()
    assert result["status"] == "queued"
    assert len(investigator.calls) == 1
    assert repo.read(incident_id)["version"] == 2
    assert len(repo.query("incident")) == 1


def actual_agent(repo, settings, governance, reason):
    settings.foundry_endpoint = "https://foundry.invalid"
    settings.reasoning_deployment = "reasoning"
    settings.embedding_endpoint = "https://embedding.invalid"
    evidence = SimpleNamespace(search=AsyncMock(return_value={"hits": [], "ru": 0}))
    memory = SimpleNamespace(recall=AsyncMock(return_value={"memories": [], "ru": 0}))
    service = AgentService(repo, settings, evidence, memory, governance)
    service._reason = reason
    return service


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", ["unknown", "no_tools", "contradictory"])
async def test_actual_agent_unsupported_proof_never_generates_fallback(
    repo, settings, setup, outcome
):
    governance, _ = setup
    if outcome == "contradictory":
        state = governance.simulation.snapshot()
        repo.patch(
            state["id"], [{"op": "set", "path": "/config_valid", "value": False}], state["_etag"]
        )
    calls = []

    async def reason(question, artifacts, recalled, tools, budget, principal, enabled):
        calls.append(outcome)
        if outcome == "contradictory":
            for function in (tools.health, tools.config, tools.backend, tools.metrics):
                await function()
        return json.dumps(
            {
                "summary": "Unverified model hypothesis",
                "root_cause": "unknown" if outcome == "unknown" else "pool_exhaustion",
                "proposed_value": None if outcome == "unknown" else 100,
                "current_proof": True,
            }
        )

    monitor = Monitor(repo, settings, governance, actual_agent(repo, settings, governance, reason))
    assert (await monitor.run_once())["status"] == "escalated"
    assert (await monitor.run_once())["status"] == "escalated"
    assert len(calls) == 1
    assert repo.query("incident")[0]["status"] == "escalated"
    assert repo.query("incident")[0]["error"] == "Unavailable"
    assert not repo.query("remediation")
    assert not repo.query("authorization")
    assert not repo.query("outbox")
    assert governance.simulation.snapshot()["pool_size"] == 50


@pytest.mark.asyncio
async def test_actual_agent_complete_current_proof_authorizes_same_target(repo, settings, setup):
    governance, _ = setup

    async def reason(question, artifacts, recalled, tools, budget, principal, enabled):
        for function in (tools.health, tools.config, tools.backend, tools.metrics):
            await function()
        return json.dumps(
            {
                "summary": "Current tools prove pool exhaustion",
                "root_cause": "pool_exhaustion",
                "proposed_value": 100,
            }
        )

    monitor = Monitor(repo, settings, governance, actual_agent(repo, settings, governance, reason))
    assert (await monitor.run_once())["status"] == "queued"
    proposal = repo.query("remediation")[0]
    assert proposal["diagnostic_proof"]["target_version"] == proposal["target_version"]
    assert proposal["target_version"] == governance.simulation.snapshot()["version"]
    assert (await monitor.run_once())["results"][0]["verified"]
    assert governance.simulation.snapshot()["pool_size"] == 100


@pytest.mark.asyncio
async def test_actual_agent_current_proof_becomes_stale_before_proposal(repo, settings, setup):
    governance, _ = setup

    async def reason(question, artifacts, recalled, tools, budget, principal, enabled):
        for function in (tools.health, tools.config, tools.backend, tools.metrics):
            await function()
        state = governance.simulation.snapshot()
        repo.patch(
            state["id"],
            [{"op": "set", "path": "/version", "value": state["version"] + 1}],
            state["_etag"],
        )
        return json.dumps(
            {
                "summary": "Old observations cannot authorize a changed target",
                "root_cause": "pool_exhaustion",
                "proposed_value": 100,
            }
        )

    monitor = Monitor(repo, settings, governance, actual_agent(repo, settings, governance, reason))
    assert (await monitor.run_once())["status"] == "escalated"
    assert (await monitor.run_once())["status"] == "escalated"
    assert not repo.query("authorization")
    assert not repo.query("outbox")
    assert governance.simulation.snapshot()["pool_size"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("changed", ["mode_generation", "target_generation"])
async def test_actual_agent_fresh_proof_cannot_cross_investigation_generation(
    repo, settings, setup, changed
):
    governance, admin = setup

    async def reason(question, artifacts, recalled, tools, budget, principal, enabled):
        if changed == "mode_generation":
            governance.set_mode("guarded", admin, governance.control()["version"])
            governance.set_mode("autonomous", admin, governance.control()["version"])
        else:
            state = governance.simulation.snapshot()
            repo.patch(
                state["id"],
                [{"op": "set", "path": "/version", "value": state["version"] + 1}],
                state["_etag"],
            )
        for function in (tools.health, tools.config, tools.backend, tools.metrics):
            await function()
        return json.dumps(
            {
                "summary": "Proof after generation changed cannot authorize the old investigation",
                "root_cause": "pool_exhaustion",
                "proposed_value": 100,
            }
        )

    monitor = Monitor(repo, settings, governance, actual_agent(repo, settings, governance, reason))
    assert (await monitor.run_once())["status"] == "escalated"
    assert (await monitor.run_once())["status"] == "escalated"
    assert not repo.query("authorization")
    assert not repo.query("outbox")
    assert governance.simulation.snapshot()["pool_size"] == 50
