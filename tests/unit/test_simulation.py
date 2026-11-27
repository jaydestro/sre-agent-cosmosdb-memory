import pytest

from app.errors import Forbidden, Invalid, Unavailable
from app.security import Principal
from app.simulation import Simulation


def operator(settings):
    return Principal("operator", frozenset({"operator"}), frozenset({settings.scope_id}))


def test_seed_is_insert_only_and_metrics_are_derived(repo, settings):
    simulation = Simulation(repo, settings)
    with pytest.raises(Unavailable):
        simulation.snapshot()
    simulation.initialize()
    original = simulation.snapshot()
    assert original["root_cause"] == "pool_exhaustion"
    assert original["metrics"]["error_rate"] > 0.4
    simulation.initialize()
    assert simulation.snapshot()["_etag"] == original["_etag"]
    changed = repo.patch(
        original["id"], [{"op": "set", "path": "/pool_size", "value": 100}], original["_etag"]
    )
    simulation.initialize()
    assert simulation.snapshot()["healthy"]
    assert simulation.snapshot()["_etag"] == changed["_etag"]


def test_injection_requires_explicit_development_human_operator(repo, settings):
    simulation = Simulation(repo, settings)
    simulation.initialize()
    with pytest.raises(Forbidden):
        simulation.inject("healthy", operator(settings))
    settings.allow_demo_reset = True
    with pytest.raises(Forbidden):
        simulation.inject(
            "healthy",
            Principal("worker", frozenset({"operator"}), frozenset({settings.scope_id}), "service"),
        )
    with pytest.raises(Invalid):
        simulation.inject("unknown", operator(settings))
    before = simulation.snapshot()["version"]
    deployed = simulation.inject("deployment_error", operator(settings))
    assert deployed["root_cause"] == "deployment_error"
    assert simulation.snapshot()["version"] == before + 1
    assert simulation.inject("healthy", operator(settings))["healthy"]


def test_seed_and_reset_refuse_other_scope(repo, settings):
    settings.scope_id = "production"
    with pytest.raises(Forbidden):
        Simulation(repo, settings).initialize()
