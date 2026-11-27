"""Scoped native Cosmos vector retrieval. Hybrid ranking is not enabled."""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone

from app.embeddings import Embeddings, validate_vector
from app.errors import Conflict, Forbidden, Invalid, Unavailable
from app.security import authorize


class EvidenceService:
    def __init__(self, repo, settings):
        self.repo, self.settings = repo, settings
        self.embeddings = Embeddings(settings)

    def _scope(self, principal, role="viewer"):
        authorize(principal, role, self.settings.scope_id)

    def _public(self, item):
        return {k: v for k, v in item.items() if k != "embedding" and not k.startswith("_")}

    def _read(self, artifact_id):
        from azure.cosmos.exceptions import CosmosResourceNotFoundError

        try:
            item = self.repo.evidence.read_item(artifact_id, partition_key=self.settings.scope_id)
        except CosmosResourceNotFoundError:
            return None
        if (
            item.get("scope_id") != self.settings.scope_id
            or item.get("service_id") != self.settings.service_id
        ):
            raise Forbidden("Evidence scope mismatch.")
        return item

    async def ingest(self, artifact: dict, principal) -> dict:
        self._scope(principal, "operator")
        self.settings.require_live("cosmos_endpoint", "embedding_endpoint")
        if not isinstance(artifact, dict) or set(artifact) - {
            "kind",
            "text",
            "image_bytes",
            "content_type",
            "title",
            "source_incident_id",
            "historical",
            "captured_at",
            "source",
        }:
            raise Invalid("Evidence must use the server-defined artifact schema.")
        kind = artifact.get("kind", "text")
        if kind not in {"text", "image"}:
            raise Invalid("Evidence kind must be text or image.")
        text = artifact.get("text", "")
        title = artifact.get("title", "")
        incident_id = artifact.get("source_incident_id")
        source = artifact.get("source", "authenticated_artifact")
        captured_at = artifact.get("captured_at")
        if (
            not isinstance(source, str)
            or len(source) > 256
            or "://" in source
            or "sig=" in source.lower()
        ):
            raise Invalid("Evidence source must be a bounded label, not a URL.")
        if type(artifact.get("historical", True)) is not bool:
            raise Invalid("Historical evidence metadata must be a boolean.")
        if captured_at is not None:
            try:
                parsed_time = datetime.fromisoformat(captured_at)
                if parsed_time.tzinfo is None:
                    raise ValueError
            except (ValueError, TypeError):
                raise Invalid("Evidence capture time must include a timezone.") from None
        if (
            not isinstance(text, str)
            or len(text) > 32000
            or not isinstance(title, str)
            or len(title) > 256
        ):
            raise Invalid("Evidence text or title exceeds safe limits.")
        if incident_id:
            incident = await asyncio.to_thread(self.repo.read, incident_id)
            if (
                not incident
                or incident.get("scope_id") != self.settings.scope_id
                or incident.get("type") != "incident"
            ):
                raise Invalid("Evidence source incident does not exist in this scope.")
        data = artifact.get("image_bytes") if kind == "image" else text.encode()
        if not isinstance(data, bytes) or not data:
            raise Invalid("Evidence content is required.")
        content_type = (
            artifact.get("content_type", "image/png") if kind == "image" else "text/plain"
        )
        metadata = {
            "scope_id": self.settings.scope_id,
            "service_id": self.settings.service_id,
            "environment": self.settings.app_env,
            "model": self.settings.embedding_model,
            "dimensions": self.settings.embedding_dimensions,
            "representation": "dense-float32",
            "kind": kind,
            "content_type": content_type,
            "title": title,
            "source_incident_id": incident_id,
            "historical": bool(artifact.get("historical", True)),
            "captured_at": captured_at,
            "source_label": source,
        }
        digest = hashlib.sha256(data).hexdigest()
        identity = hashlib.sha256(json.dumps(metadata, sort_keys=True).encode() + data).hexdigest()
        artifact_id = f"evidence:{identity}"
        cached = await asyncio.to_thread(self._read, artifact_id)
        if cached:
            return {**self._public(cached), "cached": True}
        cache_identity = hashlib.sha256(
            json.dumps(
                {
                    key: metadata[key]
                    for key in (
                        "scope_id",
                        "service_id",
                        "environment",
                        "model",
                        "dimensions",
                        "representation",
                        "kind",
                        "content_type",
                    )
                },
                sort_keys=True,
            ).encode()
            + data
        ).hexdigest()
        cache_id = f"embedding-cache:{cache_identity}"
        vector_cache = await asyncio.to_thread(self._read, cache_id)
        blob_name = None
        if kind == "image":
            extension = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}.get(
                content_type
            )
            if not extension:
                raise Invalid("Unsupported evidence image type.")
            scope_hash = hashlib.sha256(self.settings.scope_id.encode()).hexdigest()
            blob_name = f"evidence/{scope_hash}/{digest}.{extension}"
            if vector_cache is None:
                await self.embeddings.upload_image(data, blob_name, content_type)
                vector = await self.embeddings.embed_image(blob_name)
        else:
            if vector_cache is None:
                vector = await self.embeddings.embed_text(text)
        if vector_cache is not None:
            if vector_cache.get("content_hash") != digest:
                raise Conflict("Embedding cache content integrity check failed.")
            vector = vector_cache["embedding"]
        else:
            cache_item = {
                **metadata,
                "id": cache_id,
                "type": "embedding_cache",
                "content_hash": digest,
                "embedding": validate_vector(vector, self.settings.embedding_dimensions),
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            try:
                await asyncio.to_thread(self.repo.evidence.create_item, cache_item)
            except Exception as exc:
                from azure.cosmos.exceptions import CosmosResourceExistsError

                if not isinstance(exc, CosmosResourceExistsError):
                    raise Unavailable("Embedding cache persistence failed.") from None
        item = {
            **metadata,
            "id": artifact_id,
            "type": "evidence",
            "content_hash": digest,
            "text": text,
            "blob_name": blob_name,
            "image_ref": f"/api/evidence/{artifact_id}/image" if blob_name else None,
            "embedding": validate_vector(vector, self.settings.embedding_dimensions),
            "created_at": datetime.now(timezone.utc).isoformat(),
            "created_by": principal.subject,
            "source": "authenticated_artifact",
            "trust": "historical_reference" if metadata["historical"] else "unverified_observation",
        }
        try:
            await asyncio.to_thread(self.repo.evidence.create_item, item)
        except Exception as exc:
            from azure.cosmos.exceptions import CosmosResourceExistsError

            if isinstance(exc, CosmosResourceExistsError):
                return {
                    **self._public(await asyncio.to_thread(self._read, artifact_id)),
                    "cached": True,
                }
            raise Unavailable("Evidence persistence failed.") from None
        return {**self._public(item), "cached": False}

    async def image_bytes(self, artifact_id: str, principal) -> tuple[bytes, str]:
        self._scope(principal)
        self.settings.require_live("cosmos_endpoint", "blob_account_url", "blob_container")
        item = await asyncio.to_thread(self._read, artifact_id)
        if not item or item["kind"] != "image" or not item.get("blob_name"):
            raise Invalid("Image evidence was not found.")
        data, content_type = await self.embeddings.download_image(item["blob_name"])
        if hashlib.sha256(data).hexdigest() != item["content_hash"]:
            raise Conflict("Image evidence integrity check failed.")
        return data, content_type

    async def search(self, query: str, principal, top_k=None) -> dict:
        self._scope(principal)
        self.settings.require_live("cosmos_endpoint", "embedding_endpoint")
        limit = self.settings.evidence_top_k if top_k is None else top_k
        if type(limit) is not int or not 1 <= limit <= 50:
            raise Invalid("Evidence result limit must be between 1 and 50.")
        start = time.perf_counter()
        vector = await self.embeddings.embed_text(query, query=True)
        parameters = [
            {"name": "@vector", "value": vector},
            {"name": "@scope", "value": self.settings.scope_id},
            {"name": "@service", "value": self.settings.service_id},
            {"name": "@environment", "value": self.settings.app_env},
            {"name": "@model", "value": self.settings.embedding_model},
            {"name": "@dimensions", "value": self.settings.embedding_dimensions},
        ]
        sql = f"""SELECT TOP {limit} c.id,c.kind,c.title,c.text,c.image_ref,c.content_hash,
        c.historical,c.trust,c.source_incident_id,c.created_at,c.captured_at,c.source_label,
        c.scope_id,c.service_id,
        VectorDistance(c.embedding,@vector) AS distance FROM c
        WHERE c.type='evidence' AND c.scope_id=@scope AND c.service_id=@service
        AND c.environment=@environment
        AND c.model=@model AND c.dimensions=@dimensions AND c.representation='dense-float32'
        ORDER BY VectorDistance(c.embedding,@vector)"""
        ru = 0.0

        def response_hook(headers, _):
            nonlocal ru
            ru += float(headers.get("x-ms-request-charge", 0))

        def retrieve():
            return list(
                self.repo.evidence.query_items(
                    query=sql,
                    parameters=parameters,
                    partition_key=self.settings.scope_id,
                    max_item_count=limit,
                    response_hook=response_hook,
                )
            )

        try:
            hits = await asyncio.to_thread(retrieve)
        except Exception:
            raise Unavailable("Native Cosmos vector retrieval failed.") from None
        if any(
            h.get("scope_id") != self.settings.scope_id
            or h.get("service_id") != self.settings.service_id
            for h in hits
        ):
            raise Forbidden("Evidence retrieval returned an invalid scope.")
        return {
            "hits": [self._public(h) for h in hits],
            "model": self.settings.embedding_model,
            "dimensions": self.settings.embedding_dimensions,
            "query": query,
            "latency_ms": round((time.perf_counter() - start) * 1000, 2),
            "ru": ru,
            "ranking": "native_vector",
            "hybrid_enabled": False,
        }
