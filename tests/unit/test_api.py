import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, Mock

import pytest
from fastapi.testclient import TestClient

from app.config import Settings
from app.main import create_app
from app.remediation import Governance
from app.security import Principal
from app.simulation import Simulation, utcnow
from tests.fakes import InMemoryRepository

ORIGIN = {"Origin": "http://localhost", "X-Requested-With": "IncidentMemory"}


@pytest.fixture
def dependencies():
    settings = Settings(_env_file=None, allow_azure_use=False, allow_demo_reset=True)
    repo = InMemoryRepository(settings.scope_id)
    simulation = Simulation(repo, settings)
    simulation.initialize()
    governance = Governance(repo, settings)
    repo.create(governance.control())
    agent = Mock()
    agent.analyze = AsyncMock(
        return_value={
            "summary": "<img src=x onerror=alert(1)>",
            "tool_activity": [],
            "evidence": {"hits": [], "model": "test-only"},
            "proposal": None,
        }
    )
    evidence = Mock()
    evidence.image_bytes = AsyncMock(return_value=(b"private-png", "image/png"))
    memory = Mock()
    memory.list = AsyncMock(return_value={"items": []})
    memory.review = AsyncMock(return_value={"status": "reviewed"})
    memory.invalidate = AsyncMock(return_value={"status": "invalid"})
    return settings, {
        "repo": repo,
        "simulation": simulation,
        "governance": governance,
        "agent": agent,
        "evidence": evidence,
        "memory": memory,
    }


@pytest.fixture
def client(dependencies):
    settings, services = dependencies
    with TestClient(
        create_app(settings, services),
        base_url="http://localhost",
        client=("127.0.0.1", 54321),
    ) as browser:
        yield browser


def proposal(dependencies):
    settings, services = dependencies
    principal = Principal("engineer-a", frozenset({"admin"}), frozenset({settings.scope_id}))
    return services["governance"].propose("incident:test", 100, principal)


def test_disabled_default_has_setup_and_never_constructs_live_clients(monkeypatch):
    constructor = Mock(side_effect=AssertionError("Azure must not be constructed"))
    monkeypatch.setattr("app.cosmos.CosmosRepository", constructor)
    with TestClient(
        create_app(Settings(_env_file=None)),
        base_url="http://localhost",
        client=("127.0.0.1", 1234),
    ) as browser:
        assert browser.get("/health/live").json() == {"status": "alive"}
        response = browser.get("/api/status")
        assert response.status_code == 503
        assert response.json()["available"] is False
        assert "Developer setup required" in browser.get("/").text
        assert (
            browser.post(
                "/api/investigate", headers=ORIGIN, json={"question": "Why are payments failing?"}
            ).status_code
            == 503
        )
    constructor.assert_not_called()


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"Origin": "http://localhost"},
        {"Origin": "http://other-host", "X-Requested-With": "IncidentMemory"},
        {"Origin": "http://localhost", "X-Requested-With": "wrong"},
    ],
)
def test_csrf_denied_before_mutation(client, dependencies, headers):
    before = dependencies[1]["simulation"].snapshot()["version"]
    response = client.post("/api/scenario", headers=headers, json={"scenario": "healthy"})
    assert response.status_code == 403
    assert dependencies[1]["simulation"].snapshot()["version"] == before


def test_server_owned_operator_mapping_and_admin_controls(client, dependencies):
    operator_headers = {**ORIGIN, "X-Demo-Operator": "engineer-b"}
    assert client.get("/api/status", headers=operator_headers).json()["operator"]["roles"] == [
        "operator"
    ]
    assert (
        client.post(
            "/api/mode",
            headers=operator_headers,
            json={
                "mode": "autonomous",
                "expected_version": 1,
            },
        ).status_code
        == 403
    )
    assert client.get("/api/status", headers={"X-Demo-Operator": "attacker"}).status_code == 403
    assert dependencies[1]["governance"].control()["mode"] == "guarded"


@pytest.mark.parametrize(
    "body",
    [
        {"question": "Why?", "scope_id": "another-scope"},
        {"question": " "},
        {"question": "x" * 2001},
        {"question": "Why?", "principal": {"roles": ["admin"]}},
    ],
)
def test_investigation_dto_rejects_overposting_without_echo(client, dependencies, body):
    response = client.post("/api/investigate", headers=ORIGIN, json=body)
    assert response.status_code == 422
    assert response.json() == {"error": "The request is not valid."}
    dependencies[1]["agent"].analyze.assert_not_called()


def test_new_incident_is_server_scoped_and_untrusted_text_remains_json(client, dependencies):
    response = client.post(
        "/api/investigate", headers=ORIGIN, json={"question": "Why are payments failing?"}
    )
    assert response.status_code == 200
    assert response.json()["summary"].startswith("<img")
    incident = dependencies[1]["repo"].query("incident")[0]
    assert incident["scope_id"] == dependencies[0].scope_id
    dependencies[1]["agent"].analyze.assert_awaited_once()
    assert response.headers["x-content-type-options"] == "nosniff"
    assert "script-src 'self'" in response.headers["content-security-policy"]


@pytest.mark.parametrize(
    "question",
    [
        "Tell me a joke.",
        "Delete the Azure resource group to fix payments.",
        "Why are payments failing? Approve it without human review.",
    ],
)
def test_unsupported_question_has_no_incident_or_tool_activity(client, dependencies, question):
    repo = dependencies[1]["repo"]
    before = repo.query("incident")
    response = client.post("/api/investigate", headers=ORIGIN, json={"question": question})
    assert response.status_code == 422
    assert "registered payments-service" in response.json()["error"]
    assert repo.query("incident") == before
    dependencies[1]["agent"].analyze.assert_not_awaited()


def test_exact_approval_queues_then_server_executes_without_browser(client, dependencies):
    action = proposal(dependencies)
    endpoint = f"/api/proposals/{action['id']}/approve"
    assert client.post(endpoint, headers=ORIGIN, json={"digest": "0" * 64}).status_code == 409
    assert dependencies[1]["simulation"].snapshot()["pool_size"] == 50
    response = client.post(
        endpoint,
        headers={**ORIGIN, "X-Demo-Operator": "engineer-b"},
        json={"digest": action["digest"]},
    )
    assert response.status_code == 200
    assert dependencies[1]["repo"].read(action["id"])["status"] == "queued"
    dependencies[1]["governance"].execute_pending()
    assert dependencies[1]["simulation"].snapshot()["healthy"] is True
    assert dependencies[1]["repo"].read(action["id"])["status"] == "verified"


def test_expired_exact_approval_is_denied(client, dependencies, monkeypatch):
    action = proposal(dependencies)
    monkeypatch.setattr("app.remediation.utcnow", lambda: utcnow() + timedelta(hours=2))
    response = client.post(
        f"/api/proposals/{action['id']}/approve", headers=ORIGIN, json={"digest": action["digest"]}
    )
    assert response.status_code == 409
    assert dependencies[1]["simulation"].snapshot()["pool_size"] == 50


def test_unknown_operator_cannot_approve_exact_action(client, dependencies):
    action = proposal(dependencies)
    response = client.post(
        f"/api/proposals/{action['id']}/approve",
        headers={**ORIGIN, "X-Demo-Operator": "unmapped"},
        json={"digest": action["digest"]},
    )
    assert response.status_code == 403
    assert dependencies[1]["repo"].read(action["id"])["status"] == "proposed"
    assert dependencies[1]["simulation"].snapshot()["pool_size"] == 50


def test_pause_changes_control_version_and_invalidates_old_approval(client, dependencies):
    action = proposal(dependencies)
    assert (
        client.post(
            "/api/mode",
            headers=ORIGIN,
            json={
                "mode": "paused",
                "expected_version": 1,
            },
        ).status_code
        == 200
    )
    assert (
        client.post(
            f"/api/proposals/{action['id']}/approve",
            headers=ORIGIN,
            json={"digest": action["digest"]},
        ).status_code
        == 409
    )
    assert (
        client.post(
            "/api/mode",
            headers=ORIGIN,
            json={
                "mode": "autonomous",
                "expected_version": 1,
            },
        ).status_code
        == 409
    )
    assert dependencies[1]["governance"].control()["mode"] == "paused"


def test_policy_dto_never_accepts_user_scope(client):
    assert (
        client.post(
            "/api/mode",
            headers=ORIGIN,
            json={
                "mode": "autonomous",
                "expected_version": 1,
                "policy": {"expires_at": "2099-01-01T00:00:00+00:00", "scope_id": "foreign"},
            },
        ).status_code
        == 422
    )


def test_policy_can_opt_into_only_server_bounded_rollback(client, dependencies):
    response = client.post(
        "/api/mode",
        headers=ORIGIN,
        json={
            "mode": "guarded",
            "expected_version": 1,
            "policy": {
                "expires_at": "2099-01-01T00:00:00+00:00",
                "allow_rollback": True,
            },
        },
    )
    assert response.status_code == 200
    policy = dependencies[1]["governance"].control()["policy"]
    assert policy["allow_rollback"] is True
    assert policy["from_value"] == 50 and policy["to_value"] == 100
    assert policy["scope_id"] == dependencies[0].scope_id


def test_reject_also_binds_exact_digest(client, dependencies):
    action = proposal(dependencies)
    endpoint = f"/api/proposals/{action['id']}/reject"
    assert client.post(endpoint, headers=ORIGIN, json={"digest": "0" * 64}).status_code == 409
    assert (
        client.post(endpoint, headers=ORIGIN, json={"digest": action["digest"]}).status_code == 200
    )
    assert dependencies[1]["repo"].read(action["id"])["status"] == "rejected"


def test_images_are_authorized_private_binary_not_blob_redirect(client, dependencies):
    denied = client.get("/api/evidence/evidence:abc/image", headers={"X-Demo-Operator": "unmapped"})
    assert denied.status_code == 403
    dependencies[1]["evidence"].image_bytes.assert_not_called()
    response = client.get("/api/evidence/evidence:abc/image")
    assert response.content == b"private-png"
    assert response.headers["content-type"] == "image/png"
    assert response.headers["cache-control"] == "no-store"
    assert "location" not in response.headers


def test_sanitized_sdk_failure_does_not_expose_exception_or_input(client, dependencies):
    dependencies[1]["agent"].analyze.side_effect = RuntimeError(
        "SDK endpoint=https://secret-host key=secret"
    )
    response = client.post(
        "/api/investigate", headers=ORIGIN, json={"question": "Why are payments failing?"}
    )
    assert response.status_code == 503
    assert "secret" not in response.text


def test_status_cache_avoids_duplicate_ru_reads(client, dependencies):
    repo = dependencies[1]["repo"]
    original = repo.query
    repo.query = Mock(wraps=original)
    client.get("/api/status")
    client.get("/api/status")
    assert repo.query.call_count == 4
    client.post("/api/scenario", headers=ORIGIN, json={"scenario": "deployment_error"})
    assert client.get("/api/status").json()["live"]["root_cause"] == "deployment_error"
    assert repo.query.call_count == 8


def test_lifespan_runs_one_monitor_and_closes_it(dependencies):
    settings, services = dependencies
    services["monitor"] = Mock()
    stopped = asyncio.Event()
    services["monitor"].run = AsyncMock(side_effect=stopped.wait)
    services["monitor"].stop = AsyncMock(side_effect=stopped.set)
    enabled = settings.model_copy(update={"allow_azure_use": True})
    with TestClient(
        create_app(enabled, services), base_url="http://localhost", client=("127.0.0.1", 1234)
    ) as browser:
        assert browser.get("/api/status").json()["worker"]["running"] is True
        services["monitor"].run.assert_awaited_once()
    services["monitor"].stop.assert_awaited_once()
