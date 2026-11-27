"""Consented live comparison using an isolated server-created memory namespace.

This changes only the designated development payments workload. Measurements are
actual model results, never estimated token savings. Missing live support blocks.
"""

import argparse
import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

from app.config import Settings
from app.errors import Forbidden, Unavailable
from scripts.seed import consent, operator


async def evaluate(settings, confirmed):
    consent(settings, confirmed)
    if not settings.allow_demo_reset:
        raise Forbidden("Evaluation requires explicit development reset permission.")
    settings.require_live(
        "cosmos_endpoint",
        "foundry_endpoint",
        "reasoning_deployment",
        "memory_deployment",
        "memory_embedding_deployment",
        "embedding_endpoint",
        "blob_account_url",
        "blob_container",
    )
    from app.agent import AgentService
    from app.cosmos import CosmosRepository
    from app.evidence import EvidenceService
    from app.main import invoke
    from app.memory import MemoryService
    from app.remediation import Governance
    from app.security import Principal
    from app.simulation import Simulation

    if not hasattr(MemoryService, "for_evaluation"):
        raise Unavailable("Isolated native memory comparison is unsupported.")
    run_id = str(uuid4())
    repo = await asyncio.to_thread(CosmosRepository, settings)
    evidence = EvidenceService(repo, settings)
    shared_memory = MemoryService(repo, settings)
    memory = shared_memory.for_evaluation(run_id)
    governance = Governance(repo, settings)
    simulation = Simulation(repo, settings)
    human = operator(settings)
    agent = AgentService(repo, settings, evidence=evidence, memory=memory, governance=governance)
    records = []

    async def fixture(scenario):
        control = await invoke(governance.control)
        await invoke(governance.set_mode, "guarded", human, control["version"])
        return await invoke(simulation.inject, scenario, human)

    async def measure(label, scenario, memory_enabled):
        state = await invoke(simulation.snapshot)
        incident_id = f"incident:eval:{uuid4()}"
        await invoke(
            repo.create,
            {
                "id": incident_id,
                "type": "incident",
                "scope_id": settings.scope_id,
                "service_id": settings.service_id,
                "thread_id": incident_id,
                "status": "open",
                "target_version": state["version"],
                "created_at": state["observed_at"],
                "root_cause": state["root_cause"],
                "metrics": state["metrics"],
                "run_id": run_id,
            },
        )
        started = time.perf_counter()
        result = await agent.analyze(
            incident_id,
            human,
            question="Why are payments failing?",
            memory_enabled=memory_enabled,
        )
        elapsed = round((time.perf_counter() - started) * 1000, 2)
        proposed = result.get("proposal")
        expected = 100 if scenario == "pool_exhaustion" else None
        actual = proposed.get("proposed_value") if isinstance(proposed, dict) else None
        retrieval = result.get("evidence", {})
        hits = retrieval.get("hits", []) if isinstance(retrieval, dict) else retrieval
        if not result.get("model") or not hits:
            raise Unavailable("Live model/evidence comparison returned no verifiable evidence.")
        records.append(
            {
                "label": label,
                "scenario": scenario,
                "memory_enabled": memory_enabled,
                "incident_id": incident_id,
                "fixture_metrics": state["metrics"],
                "model": result.get("model"),
                "duration_ms": elapsed,
                "usage": result.get("usage"),
                "tool_order": [
                    item.get("tool", item.get("name")) for item in result.get("tool_activity", [])
                ],
                "evidence_ids": [item["id"] for item in hits],
                "retrieval": {
                    key: retrieval.get(key) for key in ("model", "latency_ms", "ru", "ranking")
                }
                if isinstance(retrieval, dict)
                else None,
                "expected_proposed_value": expected,
                "actual_proposed_value": actual,
                "expected_root_cause": state["root_cause"],
                "actual_root_cause": result.get("root_cause"),
                "diagnosis_accuracy": result.get("root_cause") == state["root_cause"],
                "decision_accuracy": actual == expected,
                "accuracy_criterion": (
                    "bounded pool correction for exhaustion; none for invalid deployment"
                ),
                "summary": result.get("summary"),
            }
        )
        return result

    try:
        if not await invoke(repo.read, f"service:{settings.service_id}"):
            raise Unavailable("Development seed is missing; evaluation never seeds or provisions.")
        await fixture("pool_exhaustion")
        baseline = await measure("initial-disabled", "pool_exhaustion", False)
        action = baseline.get("proposal")
        if not isinstance(action, dict) or not action.get("id") or not action.get("digest"):
            raise Unavailable("No exact action was proposed; verified learning comparison blocked.")
        unauthorized = Principal("evaluation-unmapped", frozenset(), frozenset())
        denied = False
        try:
            await invoke(governance.approve, action["id"], action["digest"], unauthorized)
        except Forbidden:
            denied = True
        if not denied:
            raise Unavailable("Authorization compliance failed; evaluation stopped.")
        await invoke(governance.approve, action["id"], action["digest"], human)
        await invoke(governance.execute_pending)
        resolution = await invoke(repo.read, f"resolution:{action['id']}")
        if not resolution or not resolution.get("verified"):
            raise Unavailable("Remediation was not verified; memory comparison blocked.")
        receipt = await memory.record_verified(resolution)
        if not receipt:
            raise Unavailable("Isolated verified memory recording was unavailable.")
        for repetition in (1, 2):
            await fixture("pool_exhaustion")
            for enabled in (False, True):
                await measure(f"repeat-{repetition}", "pool_exhaustion", enabled)
        await fixture("deployment_error")
        for enabled in (False, True):
            await measure("contradictory", "deployment_error", enabled)
        return {
            "run_id": run_id,
            "status": "completed",
            "records": records,
            "scope": settings.scope_id,
            "isolated_memory": True,
            "approval_authorization_compliance": denied,
            "verified_resolution_id": resolution["id"],
            "reasoning_deployment": settings.reasoning_deployment,
            "embedding_model": settings.embedding_model,
            "budgets": {
                key: getattr(settings, key)
                for key in (
                    "agent_max_steps",
                    "agent_max_tool_calls",
                    "agent_timeout_seconds",
                    "agent_max_tokens",
                    "evidence_top_k",
                    "memory_top_k",
                )
            },
            "accuracy": {
                "diagnosis": sum(record["diagnosis_accuracy"] for record in records) / len(records),
                "bounded_action": sum(record["decision_accuracy"] for record in records)
                / len(records),
            },
            "savings": "Not fabricated; compare the actual usage fields. Missing usage is null.",
            "memory_retention": "Isolated run namespace retained for audit; no broad deletion.",
        }
    finally:
        # Leave unattended execution disabled even on a failed or interrupted evaluation.
        try:
            control = await invoke(governance.control)
            await invoke(governance.set_mode, "paused", human, control["version"])
        finally:
            for service in (agent, memory, shared_memory, evidence, repo):
                close = getattr(service, "close", None)
                if close:
                    await invoke(close)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--confirm-azure-use", action="store_true")
    parser.add_argument("--output", default="data/evaluation-results.json")
    args = parser.parse_args()
    path = Path(args.output).resolve()
    output_dir = (Path.cwd() / "data").resolve()
    if (
        not path.is_relative_to(output_dir)
        or path.suffix != ".json"
        or path.name == "artifacts.json"
    ):
        parser.exit(
            2, "Output must be an evaluation JSON file inside ./data (current working directory).\n"
        )
    try:
        report = asyncio.run(evaluate(Settings(), args.confirm_azure_use))
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(
            json.dumps(
                {"status": "completed", "output": str(path), "records": len(report["records"])}
            )
        )
    except Exception:
        parser.exit(
            2,
            "Live comparison blocked or unavailable; no results or savings were fabricated. "
            "Check consent, isolated native memory support and development live setup.\n",
        )


if __name__ == "__main__":
    main()
