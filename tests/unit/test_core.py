from unittest.mock import MagicMock

import pytest
from azure.core import MatchConditions
from azure.cosmos import exceptions
from pydantic import ValidationError

from app.config import Settings
from app.cosmos import CosmosRepository, RequestCharge, response_hook, track_request_charge
from app.errors import Conflict, Forbidden, Invalid, Unavailable
from tests.fakes import InMemoryRepository


def document(id: str = "item", **kwargs) -> dict:
    return {"id": id, "scope_id": "demo-payments-development", "type": "incident", **kwargs}


def repository_with_container() -> tuple[CosmosRepository, MagicMock]:
    repo = CosmosRepository(
        Settings(_env_file=None, allow_azure_use=True, cosmos_endpoint="https://x")
    )
    container = MagicMock()
    repo._operations = container
    return repo, container


def test_safe_config_opt_in_and_explicit_keys():
    settings = Settings(_env_file=None, cosmos_key="secret-value")
    assert "secret-value" not in repr(settings)
    with pytest.raises(Unavailable):
        settings.require_live()
    with pytest.raises(Unavailable):
        Settings(_env_file=None, allow_azure_use=True).require_live()
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, cosmos_auth_mode="key", cosmos_key="")
    assert "input_value" not in str(error.value)
    Settings(
        _env_file=None, allow_azure_use=True, cosmos_endpoint="https://existing"
    ).require_live()


def test_lazy_cosmos_and_no_credential_or_network_when_disabled(monkeypatch):
    client = MagicMock()
    identity = MagicMock()
    monkeypatch.setattr("app.cosmos.CosmosClient", client)
    monkeypatch.setattr("app.cosmos.DefaultAzureCredential", identity)
    repo = CosmosRepository(Settings(_env_file=None))
    with pytest.raises(Unavailable):
        repo.create(document())
    with pytest.raises(Unavailable):
        repo.read("item")
    client.assert_not_called()
    identity.assert_not_called()
    repo.close()


def test_client_reuse_entra_without_key_fallback_and_close(monkeypatch):
    client = MagicMock()
    credential = MagicMock()
    monkeypatch.setattr("app.cosmos.CosmosClient", client)
    monkeypatch.setattr("app.cosmos.DefaultAzureCredential", credential)
    repo = CosmosRepository(
        Settings(
            _env_file=None,
            allow_azure_use=True,
            cosmos_endpoint="https://existing",
            cosmos_key="must-not-be-used",
        )
    )
    assert repo.client is repo.client
    assert repo.operations is repo.operations
    assert repo.evidence is repo.evidence
    assert client.call_args.kwargs["credential"] is credential.return_value
    assert client.call_count == credential.call_count == 1
    repo.close()
    client.return_value.close.assert_called_once()
    credential.return_value.close.assert_called_once()


def test_explicit_key_and_entra_failure_never_fallback(monkeypatch):
    client = MagicMock()
    identity = MagicMock()
    monkeypatch.setattr("app.cosmos.CosmosClient", client)
    monkeypatch.setattr("app.cosmos.DefaultAzureCredential", identity)
    repo = CosmosRepository(
        Settings(
            _env_file=None,
            allow_azure_use=True,
            cosmos_endpoint="https://existing",
            cosmos_auth_mode="key",
            cosmos_key="test-only-secret",
        )
    )
    repo.client
    assert client.call_args.kwargs["credential"] == "test-only-secret"
    identity.assert_not_called()
    repo.close()
    client.reset_mock()
    client.side_effect = exceptions.CosmosHttpResponseError(
        status_code=401, message="test-only-secret"
    )
    repo = CosmosRepository(
        Settings(
            _env_file=None,
            allow_azure_use=True,
            cosmos_endpoint="https://existing",
            cosmos_key="test-only-secret",
        )
    )
    with pytest.raises(Unavailable) as error:
        repo.client
    assert "test-only-secret" not in str(error.value)
    assert client.call_count == 1
    assert client.call_args.kwargs["credential"] is identity.return_value


def test_point_reads_missing_and_scope_boundary():
    repo, container = repository_with_container()
    container.read_item.return_value = document()
    assert repo.read("item")["id"] == "item"
    assert container.read_item.call_args.kwargs["partition_key"] == repo.scope_id
    container.read_item.side_effect = exceptions.CosmosResourceNotFoundError(
        status_code=404, message="private-sdk-body"
    )
    assert repo.read("missing") is None
    container.read_item.side_effect = None
    container.read_item.return_value = document(scope_id="other")
    with pytest.raises(Forbidden):
        repo.read("item")


def test_queries_parameterize_values_and_reject_arbitrary_filters():
    repo, container = repository_with_container()
    container.query_items.return_value = [document()]
    injection = "' OR true"
    repo.query("incident", filters={"status": injection}, limit=7)
    call = container.query_items.call_args.kwargs
    assert injection not in call["query"]
    assert call["query"].startswith("SELECT TOP 7 ")
    assert call["query"].endswith("ORDER BY c._ts DESC")
    assert {"name": "@f0", "value": injection} in call["parameters"]
    assert call["partition_key"] == repo.scope_id
    assert call["enable_cross_partition_query"] is False
    for filters, limit in [
        ({"scope_id": "other"}, 7),
        ({"status;DROP": "x"}, 7),
        ({}, True),
        ({}, 0),
        ({}, 101),
        ({"status": []}, 7),
    ]:
        with pytest.raises(Invalid):
            repo.query("incident", filters=filters, limit=limit)
    assert container.query_items.call_count == 1


def test_patch_etag_and_guarded_paths():
    repo, container = repository_with_container()
    container.patch_item.return_value = document(status="verified")
    operations = [{"op": "set", "path": "/status", "value": "verified"}]
    repo.patch("item", operations, '"etag"')
    kwargs = container.patch_item.call_args.kwargs
    assert kwargs["etag"] == '"etag"'
    assert kwargs["match_condition"] == MatchConditions.IfNotModified
    assert kwargs["partition_key"] == repo.scope_id
    for path in ["/scope_id", "/id", "/type", "/_etag", "/scope_id/child"]:
        with pytest.raises(Forbidden):
            repo.patch("item", [{"op": "set", "path": path, "value": "other"}], '"etag"')
    with pytest.raises(Invalid):
        repo.patch("item", operations, "")


def test_native_batch_shapes_etags_and_scope():
    repo, container = repository_with_container()
    container.execute_item_batch.return_value = [{"resourceBody": document()}]
    assert repo.batch([("create", (document(),))]) == [document()]
    with pytest.raises(Forbidden):
        repo.batch([("create", (document(scope_id="other"),))])
    with pytest.raises(Invalid):
        repo.batch([("replace", ("item", document()))])
    with pytest.raises(Invalid):
        repo.batch([("delete", ("item",))])
    with pytest.raises(Invalid):
        repo.batch(
            [
                (
                    "patch",
                    ("item", [{"op": "set", "path": "/status", "value": "x"}]),
                    {"filter_predicate": "FROM c"},
                )
            ]
        )


@pytest.mark.parametrize(
    "status,error_type",
    [
        (409, Conflict),
        (412, Conflict),
        (403, Forbidden),
        (400, Invalid),
        (429, Unavailable),
        (500, Unavailable),
    ],
)
def test_sdk_errors_are_redacted(status, error_type):
    repo, container = repository_with_container()
    container.create_item.side_effect = exceptions.CosmosHttpResponseError(
        status_code=status, message="credential=super-secret endpoint=private"
    )
    with pytest.raises(error_type) as error:
        repo.create(document())
    assert "super-secret" not in str(error.value)
    assert "private" not in str(error.value)
    assert error.value.__suppress_context__


def test_batch_failure_maps_actual_failed_operation():
    repo, container = repository_with_container()
    container.execute_item_batch.side_effect = exceptions.CosmosBatchOperationError(
        error_index=1,
        headers={},
        status_code=424,
        message="secret",
        operation_responses=[
            {"statusCode": 424},
            {"statusCode": 412},
        ],
    )
    with pytest.raises(Conflict):
        repo.batch([("create", (document("one"),)), ("create", (document("two"),))])


def test_transaction_rollback_deepcopy_and_etags():
    repo = InMemoryRepository()
    initial = repo.create(document(status="pending", data={"counter": 1}))
    initial["data"]["counter"] = 99
    assert repo.read("item")["data"]["counter"] == 1
    before = repo.read("item")
    with pytest.raises(Conflict):
        repo.batch(
            [
                (
                    "patch",
                    ("item", [{"op": "incr", "path": "/data/counter", "value": 1}]),
                    {"if_match_etag": before["_etag"]},
                ),
                ("create", (document("new"),)),
                ("create", (document(),)),
            ]
        )
    assert repo.read("item") == before
    assert repo.read("new") is None
    changed = repo.batch(
        [
            ("replace", ("item", document(status="done")), {"if_match_etag": before["_etag"]}),
            ("create", (document("new"),)),
        ]
    )
    assert changed[0]["_etag"] != before["_etag"]
    with pytest.raises(Conflict):
        repo.patch("item", [{"op": "set", "path": "/status", "value": "lost"}], before["_etag"])
    query = repo.query("incident")
    query[0]["status"] = "mutated"
    assert repo.read("item")["status"] == "done"


def test_ru_is_request_local_bounded_and_secret_free():
    response_hook({"x-ms-request-charge": 100}, {"secret": "not-collected"})
    with track_request_charge() as charge:
        for _ in range(105):
            response_hook({"x-ms-request-charge": "2.5", "authorization": "secret"}, {})
        assert charge.total_ru == 262.5
        assert len(charge.metadata) == 100
        assert "secret" not in repr(charge)
        with track_request_charge() as nested:
            response_hook({"x-ms-request-charge": "4"}, {})
        assert nested.total_ru == 4
        assert charge.total_ru == 262.5
        for value in ["NaN", "-1", "infinity", "garbage"]:
            response_hook({"x-ms-request-charge": value}, {})
        assert charge.total_ru == 262.5
    assert RequestCharge().total_ru == 0


def test_fake_array_patch_and_invalid_increment_rollback():
    repo = InMemoryRepository()
    initial = repo.create(document(values=[1], enabled=True))
    updated = repo.patch(
        "item",
        [
            {"op": "add", "path": "/values/1", "value": 2},
            {"op": "add", "path": "/values/-", "value": 3},
        ],
        initial["_etag"],
    )
    assert updated["values"] == [1, 2, 3]
    other = repo.create(document("other", enabled=True))
    with pytest.raises(Invalid):
        repo.batch(
            [
                (
                    "patch",
                    ("item", [{"op": "set", "path": "/status", "value": "mutated"}]),
                    {"if_match_etag": updated["_etag"]},
                ),
                (
                    "patch",
                    ("other", [{"op": "incr", "path": "/enabled", "value": 1}]),
                    {"if_match_etag": other["_etag"]},
                ),
            ]
        )
    assert repo.read("item") == updated


def test_fake_bounded_queries_return_latest_documents():
    repo = InMemoryRepository()
    first = repo.create(document("first", status="pending"))
    repo.create(document("second", status="pending"))
    assert repo.query("incident", limit=1)[0]["id"] == "second"
    repo.patch("first", [{"op": "set", "path": "/status", "value": "done"}], first["_etag"])
    assert repo.query("incident", limit=1)[0]["id"] == "first"
