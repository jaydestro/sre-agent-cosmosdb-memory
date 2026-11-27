from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from datetime import timedelta
from threading import Barrier

import pytest

from app.errors import Conflict, Forbidden, Invalid, Unavailable
from app.policy import evaluate_policy
from app.remediation import Governance, proposal_digest
from app.security import Principal
from app.simulation import utcnow
from app.tools import DiagnosticTools


@pytest.fixture
def governance(repo, settings):
    result = Governance(repo, settings)
    result.simulation.initialize()
    return result


@pytest.fixture
def admin(settings):
    return Principal("admin", frozenset({"admin"}), frozenset({settings.scope_id}))


def patch(repo, id, **values):
    doc = repo.read(id)
    return repo.patch(
        id, [{"op": "set", "path": f"/{k}", "value": v} for k, v in values.items()], doc["_etag"]
    )


def proposal(governance, admin, **kwargs):
    return governance.propose("incident:test", 100, admin, **kwargs)


def queued(governance, admin):
    value = proposal(governance, admin)
    governance.approve(value["id"], value["digest"], admin)
    return value


def test_missing_control_is_read_only_default(governance, repo):
    assert governance.control()["mode"] == "guarded"
    assert repo.read("control") is None


def test_only_human_admin_changes_modes_human_operator_approves(governance, admin, settings):
    service = Principal("worker", frozenset({"admin"}), frozenset({settings.scope_id}), "service")
    viewer = Principal("viewer", frozenset({"viewer"}), frozenset({settings.scope_id}))
    for principal in (service, viewer):
        with pytest.raises(Forbidden):
            governance.set_mode("autonomous", principal, 1)
    value = proposal(governance, admin)
    for principal in (service, viewer):
        with pytest.raises(Forbidden):
            governance.approve(value["id"], value["digest"], principal)
    assert governance.control()["mode"] == "guarded"
    with pytest.raises(Conflict):
        governance.set_mode("paused", admin, 999)


def test_digest_binds_exact_immutable_server_evidence(governance, admin, repo):
    value = proposal(governance, admin, evidence=[{"pool_size": 999, "trusted": True}])
    assert value["evidence"][0]["state"]["pool_size"] == 50
    with pytest.raises(Conflict):
        governance.approve(value["id"], "not-the-digest", admin)
    patch(repo, value["id"], proposed_value=99)
    with pytest.raises(Conflict):
        governance.approve(value["id"], value["digest"], admin)
    assert governance.simulation.snapshot()["pool_size"] == 50


@pytest.mark.asyncio
@pytest.mark.parametrize("pool_utilization", [1, True, 0.99])
async def test_complete_proof_approval_survives_cosmos_integral_json_float_normalization(
    governance, admin, repo, settings, pool_utilization
):
    tools = DiagnosticTools(repo, settings, admin, "incident:numeric-wire")
    for function in (tools.config, tools.backend, tools.metrics, tools.health):
        await function()
    proof = tools.verified_pool_proposal(100)
    value = governance.propose(
        "incident:numeric-wire",
        100,
        admin,
        evidence=[{"source": "current_diagnostic_tools", **proof["proof"]}],
    )
    wire = deepcopy(value)
    assert (
        type(wire["diagnostic_proof"]["proof"]["metrics"]["metrics"]["pool_utilization"]) is float
    )
    wire["diagnostic_proof"]["proof"]["metrics"]["metrics"]["pool_utilization"] = pool_utilization
    wire["evidence"][0]["state"]["metrics"]["pool_utilization"] = pool_utilization
    patch(repo, value["id"], evidence=wire["evidence"], diagnostic_proof=wire["diagnostic_proof"])
    if type(pool_utilization) is not int:
        with pytest.raises(Conflict):
            governance.approve(value["id"], value["digest"], admin)
        assert governance.simulation.snapshot()["pool_size"] == 50
        return
    assert governance.approve(value["id"], value["digest"], admin)["status"] == "queued"
    assert governance.execute_pending()[0]["verified"]
    assert governance.simulation.snapshot()["healthy"]


def test_atomic_human_execution_and_verified_resolution(governance, admin, repo):
    value = queued(governance, admin)
    assert governance.simulation.snapshot()["pool_size"] == 50
    result = governance.execute_pending()
    assert len(result) == 1 and result[0]["status"] == "verified"
    assert result[0]["verification"] == {
        "healthy": True,
        "applied": True,
        "observed_at": result[0]["after"]["observed_at"],
    }
    assert repo.read(f"authorization:{value['id']}")["status"] == "consumed"
    memory_event = repo.query("memory_outbox", filters={"status": "pending"})[0]
    assert memory_event["resolution_id"] == result[0]["id"]
    assert governance.execute_pending() == []
    assert governance.simulation.snapshot()["version"] == 2


@pytest.mark.parametrize("change", ["pause", "policy", "target", "authorization_expiry"])
def test_fresh_execution_barriers_never_mutate(governance, admin, repo, change):
    value = queued(governance, admin)
    if change == "pause":
        governance.set_mode("paused", admin, governance.control()["version"])
    elif change == "policy":
        policy = dict(governance.control()["policy"])
        policy["version"] += 1
        patch(repo, "control", policy=policy)
    elif change == "target":
        patch(repo, "service:payments", version=2, config_valid=False)
    else:
        patch(
            repo,
            f"authorization:{value['id']}",
            expires_at=(utcnow() - timedelta(seconds=1)).isoformat(),
        )
    governance.execute_pending()
    assert governance.simulation.snapshot()["pool_size"] == 50
    assert repo.read(value["id"])["status"] == "escalated"
    assert not repo.query("memory_outbox")


def test_expired_approval_is_durably_escalated(governance, admin, repo):
    value = proposal(governance, admin)
    value["expires_at"] = (utcnow() - timedelta(seconds=1)).isoformat()
    patch(repo, value["id"], expires_at=value["expires_at"], digest=proposal_digest(value))
    expired = governance.approve(value["id"], proposal_digest(value), admin)
    assert expired["status"] == "escalated"
    assert not repo.query("outbox")


def test_autonomous_bounded_execution_and_contradictory_evidence(governance, admin, repo):
    governance.set_mode("autonomous", admin, 1)
    value = proposal(governance, admin)
    assert governance.authorize_policy(value["id"])["status"] == "queued"
    assert governance.execute_pending()[0]["verified"]


def test_deployment_error_is_not_pool_corrected(governance, admin, repo):
    governance.set_mode("autonomous", admin, 1)
    patch(repo, "service:payments", config_valid=False)
    value = proposal(governance, admin)
    assert governance.authorize_policy(value["id"])["status"] == "escalated"
    assert governance.simulation.snapshot()["pool_size"] == 50
    assert not repo.query("outbox")


def test_policy_budget_cooldown_and_validation(governance, admin, repo):
    governance.set_mode("autonomous", admin, 1)
    value = proposal(governance, admin)
    control = governance.control()
    state = governance.simulation.snapshot()
    for changes in (
        {"attempts": 3},
        {"actions": 10},
        {"last_action_at": utcnow().isoformat()},
        {"inflight": "other"},
    ):
        decision = evaluate_policy(control["policy"], value, state, {**control, **changes})
        assert not decision["allowed"]
    policy = {**control["policy"], "to_value": 1000}
    with pytest.raises(Invalid):
        governance.set_mode("autonomous", admin, control["version"], policy)


def test_rejection_is_terminal_and_replay_is_safe(governance, admin, repo):
    value = proposal(governance, admin)
    assert governance.reject(value["id"], value["digest"], admin)["status"] == "rejected"
    with pytest.raises(Conflict):
        governance.approve(value["id"], value["digest"], admin)
    assert not repo.query("outbox")


def test_two_workers_one_effect(governance, admin, repo, settings, monkeypatch):
    value = queued(governance, admin)
    workers = [Governance(repo, settings), Governance(repo, settings)]
    batch = repo.batch
    barrier = Barrier(2)

    def synchronized(ops):
        if any(op[0] == "replace" and op[1][0] == "service:payments" for op in ops):
            barrier.wait(timeout=5)
        return batch(ops)

    monkeypatch.setattr(repo, "batch", synchronized)
    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda worker: worker.execute_pending(), workers))
    assert governance.simulation.snapshot()["version"] == 2
    assert governance.simulation.snapshot()["last_remediation"] == value["id"]
    assert len(repo.query("resolution")) == 1
    assert len(repo.query("audit", filters={"proposal_id": value["id"]})) == 4


def test_transaction_conflict_rolls_back_entire_effect(governance, admin, repo, monkeypatch):
    value = queued(governance, admin)
    batch = repo.batch
    fired = False

    def racing(ops):
        nonlocal fired
        if not fired and any(op[0] == "replace" and op[1][0] == "service:payments" for op in ops):
            fired = True
            patch(repo, "control", mode="paused", version=2)
        return batch(ops)

    monkeypatch.setattr(repo, "batch", racing)
    governance.execute_pending()
    assert governance.simulation.snapshot()["pool_size"] == 50
    governance.execute_pending()
    assert repo.read(value["id"])["status"] == "escalated"


def test_lost_apply_response_recovers_verification_without_duplicate(
    governance, admin, repo, monkeypatch
):
    value = queued(governance, admin)
    batch = repo.batch
    fired = False

    def lost_response(ops):
        nonlocal fired
        result = batch(ops)
        if not fired and any(op[0] == "replace" and op[1][0] == "service:payments" for op in ops):
            fired = True
            raise Unavailable("response lost")
        return result

    monkeypatch.setattr(repo, "batch", lost_response)
    with pytest.raises(Unavailable):
        governance.execute_pending()
    assert repo.read(f"outbox:{value['id']}")["status"] == "verifying"
    assert governance.execute_pending()[0]["verified"]
    assert governance.simulation.snapshot()["version"] == 2


def test_restart_relief_is_not_verified_root_cause_fix(governance, admin, repo):
    value = governance.propose("incident:restart", 1, admin, operation="restart")
    governance.approve(value["id"], value["digest"], admin)
    result = governance.execute_pending()[0]
    assert result["status"] == "failed"
    assert governance.simulation.snapshot()["root_cause"] == "pool_exhaustion"
    assert governance.simulation.snapshot()["restart_count"] == 1
    assert not repo.query("memory_outbox")


def test_failed_pool_verification_creates_unapproved_rollback(governance, admin, repo, monkeypatch):
    value = queued(governance, admin)
    batch = repo.batch
    changed = False

    def corrupt_after_apply(ops):
        nonlocal changed
        result = batch(ops)
        if not changed and any(op[0] == "replace" and op[1][0] == "service:payments" for op in ops):
            changed = True
            patch(repo, "service:payments", config_valid=False)
        return result

    monkeypatch.setattr(repo, "batch", corrupt_after_apply)
    assert governance.execute_pending()[0]["status"] == "failed"
    rollback = repo.read(f"remediation:rollback:{value['id']}")
    assert rollback["status"] == "proposed" and rollback["rollback_of"] == value["id"]
    governance.execute_pending()
    assert governance.simulation.snapshot()["pool_size"] == 100
    assert not repo.query("memory_outbox")


def test_lost_authorization_response_is_idempotent(governance, admin, repo, monkeypatch):
    value = proposal(governance, admin)
    batch = repo.batch
    fired = False

    def lost_response(ops):
        nonlocal fired
        result = batch(ops)
        if not fired and any(
            op[0] == "create" and op[1][0]["type"] == "authorization" for op in ops
        ):
            fired = True
            raise Unavailable("lost authorization response")
        return result

    monkeypatch.setattr(repo, "batch", lost_response)
    with pytest.raises(Unavailable):
        governance.approve(value["id"], value["digest"], admin)
    assert governance.approve(value["id"], value["digest"], admin)["status"] == "queued"
    assert len(repo.query("authorization")) == len(repo.query("outbox")) == 1
    assert governance.execute_pending()[0]["verified"]


def test_post_approval_digest_change_is_escalated_without_mutation(governance, admin, repo):
    value = queued(governance, admin)
    patch(repo, value["id"], proposed_value=99)
    governance.execute_pending()
    assert repo.read(value["id"])["status"] == "escalated"
    assert governance.simulation.snapshot()["pool_size"] == 50


def test_guarded_restore_config_requires_exact_authorization(governance, admin, repo):
    patch(repo, "service:payments", pool_size=100, config_valid=False)
    value = governance.propose("incident:config", 1, admin, operation="restore_config")
    assert governance.execute_pending() == []
    assert not governance.simulation.snapshot()["config_valid"]
    governance.approve(value["id"], value["digest"], admin)
    assert governance.execute_pending()[0]["verified"]
    assert governance.simulation.snapshot()["config_valid"]


def test_policy_disabled_after_approval_is_barrier(governance, admin, repo):
    value = queued(governance, admin)
    policy = {**governance.control()["policy"], "enabled": False}
    patch(repo, "control", policy=policy)
    governance.execute_pending()
    assert repo.read(value["id"])["status"] == "escalated"
    assert governance.simulation.snapshot()["pool_size"] == 50


def test_service_principal_proposal_is_narrow(governance, admin, settings):
    broad = Principal(
        "unrelated-service", frozenset({"admin"}), frozenset({settings.scope_id}), "service"
    )
    with pytest.raises(Forbidden):
        governance.propose("incident:broad", 100, broad)
    narrow = Principal(
        "payments-monitor", frozenset({"operator"}), frozenset({settings.scope_id}), "service"
    )
    with pytest.raises(Invalid):
        governance.propose("incident:narrow", 100, narrow)


@pytest.mark.parametrize("allow_rollback", [False, True])
def test_autonomous_rollback_requires_explicit_narrow_policy(
    governance, admin, repo, monkeypatch, allow_rollback
):
    policy = {**governance.control()["policy"], "allow_rollback": allow_rollback}
    governance.set_mode("autonomous", admin, 1, policy)
    value = proposal(governance, admin)
    governance.authorize_policy(value["id"])
    batch = repo.batch
    changed = False

    def fail_verification(ops):
        nonlocal changed
        result = batch(ops)
        if not changed and any(op[0] == "replace" and op[1][0] == "service:payments" for op in ops):
            changed = True
            patch(repo, "service:payments", config_valid=False)
        return result

    monkeypatch.setattr(repo, "batch", fail_verification)
    assert governance.execute_pending()[0]["status"] == "failed"
    patch(repo, "control", last_action_at=(utcnow() - timedelta(minutes=1)).isoformat())
    rollback_id = f"remediation:rollback:{value['id']}"
    authorization = governance.authorize_policy(rollback_id)
    assert authorization["status"] == ("queued" if allow_rollback else "escalated")
    results = governance.execute_pending()
    if allow_rollback:
        assert results[0]["status"] == "rolled_back"
        assert governance.simulation.snapshot()["pool_size"] == 50
        assert repo.read("incident:test")["status"] == "escalated"
    else:
        assert results == []
        assert governance.simulation.snapshot()["pool_size"] == 100
    assert not repo.query("memory_outbox")


def test_guarded_rollback_needs_new_exact_human_approval(governance, admin, repo, monkeypatch):
    value = queued(governance, admin)
    batch = repo.batch
    changed = False

    def fail_verification(ops):
        nonlocal changed
        result = batch(ops)
        if not changed and any(op[0] == "replace" and op[1][0] == "service:payments" for op in ops):
            changed = True
            patch(repo, "service:payments", config_valid=False)
        return result

    monkeypatch.setattr(repo, "batch", fail_verification)
    assert governance.execute_pending()[0]["status"] == "failed"
    rollback_id = f"remediation:rollback:{value['id']}"
    rollback = repo.read(rollback_id)
    assert governance.execute_pending() == []
    assert governance.simulation.snapshot()["pool_size"] == 100
    with pytest.raises(Conflict):
        governance.approve(rollback_id, value["digest"], admin)
    patch(repo, "control", last_action_at=(utcnow() - timedelta(minutes=1)).isoformat())
    assert governance.approve(rollback_id, rollback["digest"], admin)["status"] == "queued"
    assert governance.execute_pending()[0]["status"] == "rolled_back"
    assert governance.simulation.snapshot()["pool_size"] == 50
    assert repo.read(f"authorization:{rollback_id}")["status"] == "consumed"
    assert not repo.query("memory_outbox")


@pytest.mark.parametrize("changed_document", ["policy", "target"])
def test_same_version_content_changes_are_execution_barriers(
    governance, admin, repo, changed_document
):
    value = queued(governance, admin)
    if changed_document == "policy":
        policy = {**governance.control()["policy"], "cooldown_seconds": 60}
        patch(repo, "control", policy=policy)
    else:
        patch(repo, "service:payments", demand=80)
    governance.execute_pending()
    assert repo.read(value["id"])["status"] == "escalated"
    assert governance.simulation.snapshot()["pool_size"] == 50


def test_lost_verification_response_preserves_one_resolution(governance, admin, repo, monkeypatch):
    value = queued(governance, admin)
    batch = repo.batch
    fired = False

    def lost_verification(ops):
        nonlocal fired
        result = batch(ops)
        if not fired and any(op[0] == "create" and op[1][0]["type"] == "resolution" for op in ops):
            fired = True
            raise Unavailable("lost verification response")
        return result

    monkeypatch.setattr(repo, "batch", lost_verification)
    with pytest.raises(Unavailable):
        governance.execute_pending()
    assert governance.execute_pending() == []
    assert repo.read(value["id"])["status"] == "verified"
    assert len(repo.query("resolution")) == len(repo.query("memory_outbox")) == 1
    assert governance.simulation.snapshot()["version"] == 2


def test_repeated_failed_mutations_suspend_and_escalate(governance, admin, repo):
    for index in range(3):
        value = governance.propose(f"incident:restart:{index}", 1, admin, operation="restart")
        assert governance.approve(value["id"], value["digest"], admin)["status"] == "queued"
        assert governance.execute_pending()[0]["status"] == "failed"
        patch(repo, "control", last_action_at=(utcnow() - timedelta(minutes=1)).isoformat())
    assert governance.control()["failures"] == 3
    assert governance.control()["suspended"]
    assert repo.read("incident:restart:2")["status"] == "escalated"
    assert governance.simulation.snapshot()["restart_count"] == 3
    with pytest.raises(Conflict):
        governance.propose("incident:fourth", 1, admin, operation="restart")
