"""Explicit opt-in, existing dedicated development resources only; never provision."""

import base64
import os
import uuid

import pytest

from app.config import Settings
from app.cosmos import CosmosRepository
from app.embeddings import validate_vector
from app.evidence import EvidenceService
from app.memory import MemoryService
from app.security import Principal

pytestmark = [
    pytest.mark.skipif(
        os.getenv("LIVE_AZURE_TESTS", "").lower() != "true"
        or os.getenv("ALLOW_AZURE_USE", "").lower() != "true",
        reason="Live Azure tests require both explicit gates; no Azure calls are made by default.",
    ),
]


@pytest.fixture
def live_settings():
    settings = Settings()
    test_scope = os.getenv("TEST_SCOPE_ID", "")
    if (
        not test_scope.startswith("test-")
        or settings.scope_id != test_scope
        or settings.app_env != "development"
    ):
        pytest.fail("Live tests require a dedicated TEST_SCOPE_ID=test-* development scope.")
    settings.require_live(
        "cosmos_endpoint",
        "embedding_endpoint",
        "blob_account_url",
        "blob_container",
        "foundry_endpoint",
        "memory_deployment",
        "memory_embedding_deployment",
    )
    assert settings.embedding_dimensions == 1536
    return settings


@pytest.fixture
def live_repo(live_settings):
    repository = CosmosRepository(live_settings)
    yield repository
    repository.close()


@pytest.mark.asyncio
async def test_real_private_image_vector_retrieval(live_settings, live_repo):
    from azure.core.exceptions import HttpResponseError
    from azure.storage.blob import BlobClient

    principal = Principal(
        "integration-engineer", frozenset({"operator"}), frozenset({live_settings.scope_id})
    )
    evidence = EvidenceService(live_repo, live_settings)
    image = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO"
        "+ip1sAAAAASUVORK5CYII="
    )
    artifact = await evidence.ingest(
        {
            "kind": "image",
            "image_bytes": image,
            "content_type": "image/png",
            "title": "Integration private screenshot " + uuid.uuid4().hex,
            "historical": True,
        },
        principal,
    )
    assert artifact["image_ref"].startswith("/api/evidence/") and "sig=" not in str(artifact)
    data, content_type = await evidence.image_bytes(artifact["id"], principal)
    assert data == image and content_type == "image/png"
    stored = live_repo.evidence.read_item(artifact["id"], partition_key=live_settings.scope_id)
    assert len(validate_vector(stored["embedding"], 1536)) == 1536
    result = await evidence.search("A historical screenshot", principal, top_k=50)
    assert any(hit["id"] == artifact["id"] for hit in result["hits"])
    assert result["ru"] > 0
    anonymous = BlobClient(
        account_url=live_settings.blob_account_url,
        container_name=live_settings.blob_container,
        blob_name=artifact["blob_name"],
    )
    try:
        with pytest.raises(HttpResponseError) as error:
            anonymous.download_blob().readall()
        assert error.value.status_code in {401, 403, 404}
    finally:
        anonymous.close()


@pytest.mark.asyncio
async def test_real_native_memory_processing_and_fresh_scope_recall(live_settings, live_repo):
    run_id = str(uuid.uuid4())
    memory = MemoryService(live_repo, live_settings).for_evaluation(run_id)
    principal = Principal(
        "integration-engineer", frozenset({"operator"}), frozenset({live_settings.scope_id})
    )
    resolution = live_repo.create(
        {
            "id": f"resolution:test:{run_id}",
            "type": "resolution",
            "scope_id": live_settings.scope_id,
            "service_id": live_settings.service_id,
            "incident_id": f"incident:test:{run_id}",
            "thread_id": f"thread:test:{run_id}",
            "status": "verified",
            "verified": True,
            "operation": "pool_adjust",
            "before": {
                "pool_size": 50,
                "demand": 90,
                "backend_capacity": 150,
                "config_valid": True,
            },
            "after": {"pool_size": 100, "healthy": True},
            "verification": {
                "healthy": True,
                "applied": True,
                "source": "dedicated_live_test_fixture",
            },
        }
    )
    try:
        receipt = await memory.record_verified(resolution)
        assert receipt["status"] == "complete" and receipt["memory_ids"]
        fresh = MemoryService(live_repo, live_settings).for_evaluation(run_id)
        try:
            recalled = await fresh.recall(
                "payments pool exhaustion configuration capacity", principal
            )
            assert any(m["source_resolution_id"] == resolution["id"] for m in recalled["memories"])
            assert all(
                m["historical"] and m["trust"] == "verified_resolution_reference"
                for m in recalled["memories"]
            )
        finally:
            await fresh.close()
        for memory_id in receipt["memory_ids"]:
            await memory.invalidate(memory_id, principal, "Dedicated live test cleanup")
    finally:
        await memory.close()


@pytest.mark.parametrize("auth_mode", ["entra", "key"])
def test_real_existing_cosmos_auth_modes(live_settings, auth_mode):
    if auth_mode == "key" and os.getenv("LIVE_COSMOS_KEY_AUTH", "").lower() != "true":
        pytest.skip(
            "Key compatibility requires a separately preconfigured, explicitly enabled test."
        )
    if auth_mode == "key" and not live_settings.cosmos_key.get_secret_value():
        pytest.skip("No preconfigured Cosmos key; keys are never fetched or enabled by tests.")
    settings = live_settings.model_copy(update={"cosmos_auth_mode": auth_mode})
    repository = CosmosRepository(settings)
    try:
        assert isinstance(repository.query("incident", limit=1), list)
    finally:
        repository.close()
