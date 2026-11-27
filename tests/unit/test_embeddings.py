import hashlib
import io
import json
import sys
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock
from urllib.parse import parse_qs

import pytest
from azure.core.credentials import AccessToken
from azure.core.pipeline.transport import HttpResponse, HttpTransport
from azure.cosmos.exceptions import CosmosResourceNotFoundError
from azure.data.ai import InferenceClient
from azure.data.ai.models import EmbeddingResult, MultimodalDenseEmbedding

from app.config import Settings
from app.embeddings import Embeddings, validate_vector
from app.errors import Forbidden, Invalid, Unavailable
from app.evidence import EvidenceService
from app.security import Principal


def principal(scope="demo-payments-development"):
    return Principal("engineer", frozenset({"operator"}), frozenset({scope}))


def sdk_result(content_kind="text"):
    return {
        "object": "list",
        "model": "microsoft-embedding-1-large",
        "outputDataKind": "float32",
        "data": [
            {
                "kind": "multimodal_dense",
                "index": 0,
                "contentKind": content_kind,
                "vectorKind": "dense",
                "embedding": [0.1] * 1536,
            }
        ],
        "usage": {"kind": "multimodal", "promptTokens": 3, "totalTokens": 3},
    }


class SDKResponse(HttpResponse):
    def __init__(self, request, payload):
        super().__init__(request, None)
        self.status_code = 200
        self.headers = {"Content-Type": "application/json"}
        self.content_type = "application/json"
        self.payload = payload

    def body(self):
        return json.dumps(self.payload).encode()

    def json(self):
        return self.payload


class SDKTransport(HttpTransport):
    def __init__(self, payload):
        self.payload = payload
        self.requests = []

    def open(self):
        pass

    def close(self):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def send(self, request, **kwargs):
        self.requests.append(request)
        return SDKResponse(request, self.payload)


@pytest.mark.parametrize("kind", ["text", "query", "image"])
def test_actual_sdk_serializes_app_request_and_deserializes_dense_response(monkeypatch, kind):
    service = Embeddings(Settings(embedding_endpoint="https://inference.invalid"))
    credential = MagicMock()
    credential.__enter__.return_value = credential
    credential.get_token.return_value = AccessToken("offline-test-token", 4102444800)
    monkeypatch.setattr(service, "_credential", lambda: credential)
    transport = SDKTransport(sdk_result(kind))
    monkeypatch.setattr(
        "azure.data.ai.InferenceClient",
        lambda **kwargs: InferenceClient(**kwargs, transport=transport),
    )
    if kind == "image":
        storage = MagicMock()
        storage.__enter__.return_value = storage
        container = storage.get_container_client.return_value
        container.get_container_properties.return_value = {"public_access": None}
        container.get_blob_client.return_value.url = (
            "https://testaccount.blob.core.windows.net/private/image.png"
        )
        monkeypatch.setattr(service, "_storage", lambda caller: storage)
        monkeypatch.setattr(
            "azure.storage.blob.generate_blob_sas", lambda **kwargs: "sp=r&spr=https&sig=offline"
        )
        vector = service._embed(blob_name="evidence/" + "a" * 64 + "/" + "b" * 64 + ".png")
    else:
        vector = service._embed(text="pool exhausted", query=kind == "query")
    assert vector == validate_vector([0.1] * 1536, 1536)
    assert len(transport.requests) == 1
    request = transport.requests[0]
    assert request.url.startswith("https://inference.invalid/")
    body = json.loads(request.body)
    assert body["model"] == "microsoft-embedding-1-large"
    assert body["dimensions"] == 1536
    assert body["outputDataKind"] == "float32" and body["encodingFormat"] == "float"
    assert body.get("vectorKind", "dense") == "dense"
    assert body["inputs"][0]["kind"] == kind
    if kind == "image":
        assert body["inputs"][0]["sasToken"] == "sp=r&spr=https&sig=offline"
        assert "?" not in body["inputs"][0]["url"]
    else:
        assert body["inputs"][0]["content"] == "pool exhausted"
    parsed = EmbeddingResult(transport.payload)
    assert isinstance(parsed.data[0], MultimodalDenseEmbedding) and parsed.data[0].index == 0


@pytest.mark.parametrize("mismatch", ["float16", "dense_multi", "multi"])
def test_actual_sdk_rejects_incompatible_response_representation(mismatch):
    payload = sdk_result()
    if mismatch == "float16":
        payload["outputDataKind"] = "float16"
    else:
        payload["data"][0].update(
            kind="multimodal_dense_multi" if mismatch == "dense_multi" else "multimodal_multi",
            vectorKind=mismatch,
            shape=[1, 1536],
            multiEmbedding=[[0.1] * 1536],
        )
    with pytest.raises(Invalid):
        Embeddings(Settings())._read_result(EmbeddingResult(payload))


@pytest.mark.parametrize("warning_location", [None, "envelope", "file"])
def test_actual_sdk_linked_result_integrity_and_warnings(monkeypatch, warning_location):
    payload = sdk_result("image")
    warning = {"code": "EMBEDDING_INPUT_TRUNCATED", "index": 0, "message": "truncated"}
    if warning_location == "file":
        payload["warnings"] = [warning]
    encoded = json.dumps(payload).encode()
    envelope = sdk_result("image")
    envelope["data"] = []
    envelope["output"] = {
        "id": "offline",
        "url": "https://testaccount.blob.core.windows.net/results/embedding.json",
        "sasToken": "sp=r&sig=offline",
        "expiresAt": "2100-01-01T00:00:00Z",
        "recordCount": 1,
        "sizeInBytes": len(encoded),
        "sha256": hashlib.sha256(encoded).hexdigest(),
        "contentType": "application/json",
    }
    if warning_location == "envelope":
        envelope["warnings"] = [warning]
    opener = MagicMock()
    opener.open.return_value = io.BytesIO(encoded)
    monkeypatch.setattr("urllib.request.build_opener", MagicMock(return_value=opener))
    service = Embeddings(Settings())
    if warning_location:
        with pytest.warns(RuntimeWarning), pytest.raises(Invalid, match="warnings"):
            service._read_result(EmbeddingResult(envelope))
        if warning_location == "envelope":
            opener.open.assert_not_called()
    else:
        vector = service._read_result(EmbeddingResult(envelope))
        assert vector == validate_vector([0.1] * 1536, 1536)
        opener.open.assert_called_once_with(
            envelope["output"]["url"] + "?sp=r&sig=offline", timeout=30
        )


@pytest.mark.parametrize(
    "vector",
    [
        [0.0] * 1535,
        [float("nan")] * 1536,
        [float("inf")] * 1536,
        [True] * 1536,
        [1e40] * 1536,
    ],
)
def test_vector_rejects_incompatible_dimensions_or_float32(vector):
    with pytest.raises(Invalid):
        validate_vector(vector, 1536)


def test_vector_is_finite_float32_and_truncation_rejected():
    embedding = Embeddings(Settings())
    vector = validate_vector([0.1] * 1536, 1536)
    assert vector[0] != 0.1
    response = SimpleNamespace(
        model="microsoft-embedding-1-large",
        output_data_kind="float32",
        output=None,
        warnings=[],
        data=[
            SimpleNamespace(
                index=0,
                kind="multimodal_dense",
                vector_kind="dense",
                embedding=vector,
                truncated=True,
            )
        ],
    )
    with pytest.warns(RuntimeWarning), pytest.raises(Invalid):
        embedding._read_result(response)


@pytest.mark.asyncio
async def test_azure_guard_precedes_credentials(monkeypatch):
    embedding = Embeddings(Settings())
    credential = MagicMock()
    monkeypatch.setattr(embedding, "_credential", credential)
    with pytest.raises(Unavailable):
        await embedding.embed_text("payments")
    credential.assert_not_called()


def test_private_container_rejects_anonymous_access():
    embedding = Embeddings(Settings())
    storage = MagicMock()
    storage.get_container_client.return_value.get_container_properties.return_value = {
        "public_access": "blob"
    }
    with pytest.raises(Invalid):
        embedding._private_container(storage)


def test_same_credential_read_only_five_minute_user_delegation_sas(monkeypatch):
    from azure.storage.blob import UserDelegationKey

    settings = Settings()
    service = Embeddings(settings)
    credential = MagicMock()
    credential.__enter__.return_value = credential
    service._credential = lambda: credential
    storage = MagicMock()
    storage.__enter__.return_value = storage
    storage.account_name = "testaccount"
    key = UserDelegationKey()
    key.signed_oid = "00000000-0000-0000-0000-000000000001"
    key.signed_tid = "00000000-0000-0000-0000-000000000002"
    key.signed_start = "2026-11-26T00:00:00Z"
    key.signed_expiry = "2026-11-27T00:00:00Z"
    key.signed_service = "b"
    key.signed_version = "2025-11-05"
    key.value = "a2V5"
    storage.get_user_delegation_key.return_value = key
    container = storage.get_container_client.return_value
    container.get_container_properties.return_value = {"public_access": None}
    container.get_blob_client.return_value.url = (
        "https://testaccount.blob.core.windows.net/private/image.png"
    )
    storage_factory = MagicMock(return_value=storage)
    service._storage = storage_factory
    inference = MagicMock()
    inference.__enter__.return_value = inference
    inference.get_embeddings.return_value = SimpleNamespace(
        model=settings.embedding_model,
        output_data_kind="float32",
        output=None,
        warnings=[],
        data=[
            SimpleNamespace(
                index=0,
                kind="multimodal_dense",
                vector_kind="dense",
                embedding=[0.0] * 1536,
                truncated=False,
            )
        ],
    )
    inference_factory = MagicMock(return_value=inference)
    monkeypatch.setitem(
        sys.modules, "azure.data.ai", SimpleNamespace(InferenceClient=inference_factory)
    )
    service._embed(blob_name="evidence/" + "a" * 64 + "/" + "b" * 64 + ".png")
    assert inference_factory.call_args.kwargs["credential"] is credential
    storage_factory.assert_called_once_with(credential)
    kwargs = inference.get_embeddings.call_args.kwargs
    image = kwargs["inputs"][0]
    assert image["kind"] == "image" and "?" not in image["url"]
    token = parse_qs(image["sasToken"])
    assert token["sp"] == ["r"] and token["spr"] == ["https"]
    expiry = datetime.fromisoformat(token["se"][0].replace("Z", "+00:00"))
    assert 0 < (expiry - datetime.now(timezone.utc)).total_seconds() <= 300
    assert kwargs["dimensions"] == 1536 and kwargs["output_data_kind"] == "float32"


@pytest.mark.parametrize(
    "url",
    [
        "http://account.blob.core.windows.net/result.json",
        "https://127.0.0.1/result.json",
        "https://account.blob.core.windows.net.evil.invalid/result.json",
        "https://account.blob.core.windows.net/result.json?sig=credential",
        "https://user:pass@account.blob.core.windows.net/result.json",
    ],
)
def test_result_file_rejects_ssrf_and_signed_url_metadata_before_download(monkeypatch, url):
    monkeypatch.setitem(
        sys.modules, "azure.data.ai.models", SimpleNamespace(EmbeddingResult=MagicMock())
    )
    opener = MagicMock()
    monkeypatch.setattr("urllib.request.build_opener", opener)
    response = SimpleNamespace(
        model="microsoft-embedding-1-large",
        output_data_kind="float32",
        warnings=[],
        data=[],
        output=SimpleNamespace(
            url=url, sas_token="secret", size_in_bytes=12, record_count=1, sha256="a" * 64
        ),
    )
    with pytest.raises(Invalid, match="metadata"):
        Embeddings(Settings())._read_result(response)
    opener.assert_not_called()


@pytest.mark.parametrize(
    "name", ["https://example.com/image.png?sig=secret", "../image.png", "evidence/client/path.png"]
)
def test_blob_path_is_server_owned(name):
    with pytest.raises(Invalid):
        Embeddings._blob_name(name)


@pytest.mark.asyncio
async def test_ingest_hash_cache_does_not_reembed_or_expose_vector():
    settings = Settings(
        allow_azure_use=True,
        cosmos_endpoint="https://cosmos.invalid",
        embedding_endpoint="https://embedding.invalid",
    )
    store = {}
    container = MagicMock()

    def read(item, partition_key):
        assert partition_key == settings.scope_id
        if item not in store:
            raise CosmosResourceNotFoundError()
        return store[item]

    container.read_item.side_effect = read
    container.create_item.side_effect = lambda item: store.setdefault(item["id"], item)
    service = EvidenceService(SimpleNamespace(evidence=container), settings)
    service.embeddings.embed_text = AsyncMock(return_value=[0.1] * 1536)
    first = await service.ingest({"kind": "text", "text": "pool exhausted"}, principal())
    second = await service.ingest({"kind": "text", "text": "pool exhausted"}, principal())
    assert first["id"] == second["id"] and second["cached"]
    assert "embedding" not in first
    service.embeddings.embed_text.assert_awaited_once()
    changed_metadata = await service.ingest(
        {"kind": "text", "text": "pool exhausted", "title": "Another incident"},
        principal(),
    )
    assert changed_metadata["id"] != first["id"]
    service.embeddings.embed_text.assert_awaited_once()
    with pytest.raises(Invalid):
        await service.ingest({"kind": "text", "text": "x", "scope_id": "other"}, principal())
    with pytest.raises(Forbidden):
        await service.ingest({"kind": "text", "text": "x"}, principal("other"))


@pytest.mark.asyncio
async def test_image_uploaded_before_embedding_only_stable_refs_returned():
    settings = Settings(
        allow_azure_use=True,
        cosmos_endpoint="https://cosmos.invalid",
        embedding_endpoint="https://embedding.invalid",
    )
    repo = SimpleNamespace(evidence=MagicMock())
    repo.evidence.read_item.side_effect = CosmosResourceNotFoundError()
    service = EvidenceService(repo, settings)
    calls = []

    async def upload(data, name, content_type):
        calls.append(("upload", name))

    async def embed(name):
        calls.append(("embed", name))
        return [0.0] * 1536

    service.embeddings.upload_image = upload
    service.embeddings.embed_image = embed
    image = b"\x89PNG\r\n\x1a\nfixture"
    result = await service.ingest({"kind": "image", "image_bytes": image}, principal())
    assert [c[0] for c in calls] == ["upload", "embed"]
    assert calls[0][1] == calls[1][1]
    assert hashlib.sha256(image).hexdigest() in result["blob_name"]
    assert result["image_ref"].startswith("/api/evidence/")
    assert "?" not in result["image_ref"] and "sig=" not in str(result)


@pytest.mark.asyncio
async def test_search_native_scoped_sql_no_local_fallback():
    settings = Settings(
        allow_azure_use=True,
        cosmos_endpoint="https://cosmos.invalid",
        embedding_endpoint="https://embedding.invalid",
    )
    container = MagicMock()
    container.query_items.return_value = []
    service = EvidenceService(SimpleNamespace(evidence=container), settings)
    service.embeddings.embed_text = AsyncMock(return_value=[0.0] * 1536)
    result = await service.search("payments", principal(), 3)
    kwargs = container.query_items.call_args.kwargs
    assert "TOP 3" in kwargs["query"] and "ORDER BY VectorDistance" in kwargs["query"]
    assert "SELECT *" not in kwargs["query"]
    assert kwargs["partition_key"] == settings.scope_id
    assert all(
        field in kwargs["query"]
        for field in [
            "scope_id",
            "service_id",
            "environment",
            "model",
            "dimensions",
            "representation",
        ]
    )
    assert result["hits"] == [] and result["ranking"] == "native_vector"
    service.embeddings.embed_text.assert_awaited_once_with("payments", query=True)
    container.query_items.side_effect = RuntimeError("internal secret")
    with pytest.raises(Unavailable, match="Native Cosmos"):
        await service.search("payments", principal())
