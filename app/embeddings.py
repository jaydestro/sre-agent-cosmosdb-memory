"""Microsoft Embedding 1, private Blob inputs, and validated dense output."""

import asyncio
import hashlib
import json
import math
import re
import struct
import urllib.request
import warnings
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from app.errors import Invalid, Unavailable

MAX_IMAGE_BYTES = 8 * 1024 * 1024
MAX_OUTPUT_BYTES = 16 * 1024 * 1024


def validate_vector(vector, dimensions: int) -> list[float]:
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise Invalid("Embedding dimensions do not match the configured vector policy.")
    result = []
    for value in vector:
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or not math.isfinite(value)
        ):
            raise Invalid("Embedding values must be finite numbers.")
        try:
            result.append(struct.unpack("f", struct.pack("f", value))[0])
        except (OverflowError, struct.error):
            raise Invalid("Embedding values must fit float32.") from None
        if not math.isfinite(result[-1]):
            raise Invalid("Embedding values must fit float32.")
    return result


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Embeddings:
    def __init__(self, settings):
        if settings.embedding_dimensions != 1536:
            raise Invalid("Evidence vectors require the preprovisioned 1536-dimensional policy.")
        if settings.embedding_model != "microsoft-embedding-1-large":
            raise Invalid(
                "Evidence requires Microsoft Embedding 1, not the memory embedding model."
            )
        self.settings = settings

    def _credential(self):
        from azure.identity import DefaultAzureCredential

        return DefaultAzureCredential()

    def _storage(self, credential):
        from azure.storage.blob import BlobServiceClient

        parsed = urlsplit(self.settings.blob_account_url)
        if (
            parsed.scheme != "https"
            or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", parsed.hostname or "")
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
            or parsed.port not in (None, 443)
            or parsed.path not in ("", "/")
        ):
            raise Invalid("A plain Azure Blob account URL is required.")
        return BlobServiceClient(account_url=self.settings.blob_account_url, credential=credential)

    def _private_container(self, storage):
        container = storage.get_container_client(self.settings.blob_container)
        properties = container.get_container_properties()
        if properties.get("public_access"):
            raise Invalid("Image evidence requires an existing private Blob container.")
        return container

    @staticmethod
    def _blob_name(name):
        if not isinstance(name, str) or not re.fullmatch(
            r"evidence/[0-9a-f]{64}/[0-9a-f]{64}\.(png|jpg|webp)", name
        ):
            raise Invalid("Invalid server-owned evidence blob name.")
        return name

    async def upload_image(self, data: bytes, name: str, content_type: str = "image/png"):
        self.settings.require_live("blob_account_url", "blob_container")
        self._blob_name(name)
        if not isinstance(data, bytes) or not 0 < len(data) <= MAX_IMAGE_BYTES:
            raise Invalid("Image evidence must contain at most 8 MiB.")
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise Invalid("Only PNG, JPEG, and WebP evidence is supported.")
        valid_signature = {
            "image/png": data.startswith(b"\x89PNG\r\n\x1a\n"),
            "image/jpeg": data.startswith(b"\xff\xd8\xff"),
            "image/webp": data.startswith(b"RIFF") and data[8:12] == b"WEBP",
        }[content_type]
        if not valid_signature:
            raise Invalid("Image bytes do not match the declared image format.")

        def upload():
            from azure.core.exceptions import ResourceExistsError
            from azure.storage.blob import ContentSettings

            with self._credential() as credential, self._storage(credential) as storage:
                container = self._private_container(storage)
                try:
                    container.upload_blob(
                        name,
                        data,
                        overwrite=False,
                        content_settings=ContentSettings(content_type=content_type),
                    )
                except ResourceExistsError:
                    existing = container.get_blob_client(name).download_blob().readall()
                    if hashlib.sha256(existing).digest() != hashlib.sha256(data).digest():
                        raise Invalid(
                            "An existing evidence blob has conflicting content."
                        ) from None
            return {"blob_name": name, "content_type": content_type, "size": len(data)}

        try:
            return await asyncio.to_thread(upload)
        except (Invalid, Unavailable):
            raise
        except Exception:
            raise Unavailable("Private image upload failed.") from None

    async def download_image(self, name: str) -> tuple[bytes, str]:
        self.settings.require_live("blob_account_url", "blob_container")
        self._blob_name(name)

        def download():
            with self._credential() as credential, self._storage(credential) as storage:
                blob = self._private_container(storage).get_blob_client(name)
                props = blob.get_blob_properties()
                content_type = props.content_settings.content_type
                if not 0 < props.size <= MAX_IMAGE_BYTES or content_type not in {
                    "image/png",
                    "image/jpeg",
                    "image/webp",
                }:
                    raise Invalid("Stored image evidence exceeds safe limits.")
                data = blob.download_blob().readall()
                if len(data) != props.size:
                    raise Invalid("Stored image evidence is incomplete.")
                return data, content_type

        try:
            return await asyncio.to_thread(download)
        except (Invalid, Unavailable):
            raise
        except Exception:
            raise Unavailable("Private image download failed.") from None

    def _read_result(self, result):
        if result.model != self.settings.embedding_model:
            raise Invalid("The embedding service returned an incompatible model.")
        if result.output_data_kind != "float32":
            raise Invalid("The embedding service returned an incompatible numeric representation.")
        if result.warnings or any(getattr(item, "truncated", False) for item in result.data or []):
            warnings.warn(
                "Embedding input was truncated or produced a service warning; evidence rejected.",
                RuntimeWarning,
            )
            raise Invalid("Embedding warnings require reviewing the source before ingestion.")
        if result.output is not None:
            from azure.data.ai.models import EmbeddingResult

            output = result.output
            url = urlsplit(output.url)
            if (
                url.scheme != "https"
                or not re.fullmatch(r"[a-z0-9]{3,24}\.blob\.core\.windows\.net", url.hostname or "")
                or url.username
                or url.password
                or url.port not in (None, 443)
                or url.query
                or url.fragment
                or not output.sas_token
                or not isinstance(output.size_in_bytes, int)
                or not 0 < output.size_in_bytes <= MAX_OUTPUT_BYTES
                or output.record_count != 1
                or re.fullmatch(r"[0-9a-fA-F]{64}", output.sha256 or "") is None
            ):
                raise Invalid("Invalid embedding result-file metadata.")
            try:
                opener = urllib.request.build_opener(_NoRedirect())
                with opener.open(
                    f"{output.url}?{output.sas_token.lstrip('?')}", timeout=30
                ) as response:
                    payload = response.read(output.size_in_bytes + 1)
            except Exception:
                raise Unavailable("Embedding result download failed; retry the request.") from None
            if (
                len(payload) != output.size_in_bytes
                or hashlib.sha256(payload).hexdigest() != output.sha256.lower()
            ):
                raise Invalid("Embedding result-file integrity check failed.")
            downloaded = EmbeddingResult(json.loads(payload))
            if downloaded.output is not None or downloaded.model != result.model:
                raise Invalid("Invalid embedding result-file contents.")
            return self._read_result(downloaded)
        if (
            not isinstance(result.data, list)
            or len(result.data) != 1
            or result.data[0].index != 0
            or result.data[0].kind != "multimodal_dense"
            or result.data[0].vector_kind != "dense"
        ):
            raise Invalid("Expected exactly one ordered dense embedding.")
        return validate_vector(result.data[0].embedding, self.settings.embedding_dimensions)

    def _embed(self, text=None, query=False, blob_name=None):
        from azure.data.ai import InferenceClient

        with (
            self._credential() as credential,
            InferenceClient(
                endpoint=self.settings.embedding_endpoint, credential=credential
            ) as client,
        ):
            if blob_name is not None:
                from azure.storage.blob import BlobSasPermissions, generate_blob_sas

                with self._storage(credential) as storage:
                    container = self._private_container(storage)
                    now = datetime.now(timezone.utc)
                    start, expiry = now - timedelta(minutes=1), now + timedelta(minutes=5)
                    key = storage.get_user_delegation_key(start, expiry)
                    token = generate_blob_sas(
                        account_name=storage.account_name,
                        container_name=self.settings.blob_container,
                        blob_name=blob_name,
                        user_delegation_key=key,
                        permission=BlobSasPermissions(read=True),
                        start=start,
                        expiry=expiry,
                        protocol="https",
                    )
                    inputs = [
                        {
                            "kind": "image",
                            "url": container.get_blob_client(blob_name).url,
                            "sasToken": token,
                        }
                    ]
            else:
                inputs = [{"kind": "query" if query else "text", "content": text}]
            response = client.get_embeddings(
                model=self.settings.embedding_model,
                inputs=inputs,
                dimensions=self.settings.embedding_dimensions,
                encoding_format="float",
                output_data_kind="float32",
            )
            return self._read_result(response)

    async def embed_text(self, text: str, query: bool = False) -> list[float]:
        self.settings.require_live("embedding_endpoint")
        if not isinstance(text, str) or not text.strip() or len(text) > 32000:
            raise Invalid("Embedding text must be nonempty and bounded.")
        return await self._call(text=text, query=query)

    async def embed_image(self, blob_name: str) -> list[float]:
        self.settings.require_live("embedding_endpoint", "blob_account_url", "blob_container")
        return await self._call(blob_name=self._blob_name(blob_name))

    async def _call(self, **kwargs):
        try:
            return await asyncio.to_thread(self._embed, **kwargs)
        except (Invalid, Unavailable):
            raise
        except Exception:
            raise Unavailable("Embedding generation failed.") from None
