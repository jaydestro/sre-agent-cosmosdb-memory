import json
import math
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from dataclasses import dataclass, field
from threading import RLock
from typing import Any, Protocol, TypeVar

from azure.core import MatchConditions
from azure.core.exceptions import AzureError
from azure.cosmos import CosmosClient, exceptions
from azure.cosmos.container import ContainerProxy
from azure.identity import DefaultAzureCredential

from app.config import Settings
from app.errors import ApplicationError, Conflict, Forbidden, Invalid, Unavailable

Document = dict[str, Any]
_T = TypeVar("_T")
QUERY_FILTERS = frozenset(
    {
        "service_id",
        "incident_id",
        "status",
        "action_id",
        "run_id",
        "approval_id",
        "proposal_id",
        "mode",
        "subject",
        "category",
        "attempt_id",
    }
)


class Repository(Protocol):
    scope_id: str

    def read(self, id: str) -> Document | None: ...
    def create(self, document: Document) -> Document: ...
    def query(
        self, kind: str, *, filters: dict | None = None, limit: int = 50
    ) -> list[Document]: ...
    def batch(self, operations: list[tuple]) -> list[Document]: ...
    def patch(self, id: str, operations: list[dict], etag: str) -> Document: ...


def validate_id(id: str) -> None:
    if (
        not isinstance(id, str)
        or not id
        or len(id.encode("utf-8")) > 1023
        or any(c in id for c in "/\\?#")
        or any(ord(c) < 32 for c in id)
    ):
        raise Invalid("A valid document identifier is required.")


def validate_document(document: Document, scope_id: str) -> Document:
    if not isinstance(document, dict):
        raise Invalid("A document object is required.")
    validate_id(document.get("id", ""))
    if document.get("scope_id") != scope_id:
        raise Forbidden("Documents must belong to the configured scope.")
    if not isinstance(document.get("type"), str) or not document["type"]:
        raise Invalid("A document type is required.")
    body = deepcopy(document)
    for key in ("_etag", "_rid", "_self", "_attachments", "_ts"):
        body.pop(key, None)
    try:
        encoded = json.dumps(body, allow_nan=False).encode("utf-8")
    except (ValueError, TypeError, RecursionError):
        raise Invalid("The document must contain valid JSON.") from None
    if len(encoded) > 1_900_000:
        raise Invalid("The document exceeds the application size limit.")
    return body


def validate_query(kind: str, filters: dict | None, limit: int) -> dict:
    if not isinstance(kind, str) or not kind or len(kind) > 100:
        raise Invalid("A bounded document type is required.")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise Invalid("Query limits must be between 1 and 100.")
    filters = {} if filters is None else filters
    if not isinstance(filters, dict) or not filters.keys() <= QUERY_FILTERS:
        raise Invalid("The requested query filter is not supported.")
    if any(not isinstance(v, (str, int, float, bool, type(None))) for v in filters.values()):
        raise Invalid("Query filter values must be scalar.")
    if any(isinstance(v, float) and not math.isfinite(v) for v in filters.values()):
        raise Invalid("Query filter values must be finite.")
    return filters


def validate_patch(operations: list[dict], etag: str) -> None:
    if not isinstance(etag, str) or not etag:
        raise Invalid("An ETag precondition is required.")
    if not isinstance(operations, list) or not 1 <= len(operations) <= 10:
        raise Invalid("A patch must contain between 1 and 10 operations.")
    for op in operations:
        if not isinstance(op, dict) or op.get("op") not in {
            "add",
            "set",
            "replace",
            "remove",
            "incr",
        }:
            raise Invalid("The requested patch operation is not supported.")
        path = op.get("path")
        if not isinstance(path, str) or not path.startswith("/") or path == "/":
            raise Invalid("A valid patch path is required.")
        root = path.split("/")[1].replace("~1", "/").replace("~0", "~")
        if root in {"id", "scope_id", "type"} or root.startswith("_"):
            raise Forbidden("Document identity and scope cannot be patched.")
        if op["op"] != "remove" and "value" not in op:
            raise Invalid("A patch value is required.")
        try:
            json.dumps(op, allow_nan=False)
        except (TypeError, ValueError):
            raise Invalid("Patch operations must contain valid JSON.") from None


def validate_batch(operations: list[tuple], scope_id: str) -> list[tuple]:
    if not isinstance(operations, list) or not 1 <= len(operations) <= 100:
        raise Invalid("A batch must contain between 1 and 100 operations.")
    result: list[tuple] = []
    for operation in operations:
        if not isinstance(operation, tuple) or len(operation) not in {2, 3}:
            raise Invalid("A native SDK batch operation is required.")
        name, args = operation[:2]
        kwargs = operation[2] if len(operation) == 3 else {}
        if not isinstance(args, tuple) or not isinstance(kwargs, dict):
            raise Invalid("A native SDK batch operation is required.")
        if kwargs.keys() - {"if_match_etag"}:
            raise Invalid("Unsupported batch conditions.")
        if name == "create" and len(args) == 1 and not kwargs:
            result.append(("create", (validate_document(args[0], scope_id),)))
        elif name == "replace" and len(args) == 2:
            validate_id(args[0])
            body = validate_document(args[1], scope_id)
            if body["id"] != args[0]:
                raise Forbidden("Replacement identity cannot change.")
            if not isinstance(kwargs.get("if_match_etag"), str) or not kwargs["if_match_etag"]:
                raise Invalid("An ETag precondition is required.")
            result.append(("replace", (args[0], body), deepcopy(kwargs)))
        elif name == "patch" and len(args) == 2:
            validate_id(args[0])
            validate_patch(args[1], kwargs.get("if_match_etag", ""))
            result.append(("patch", deepcopy(args), deepcopy(kwargs)))
        else:
            raise Invalid("Unsupported batch operation.")
    return result


@dataclass
class RequestCharge:
    total_ru: float = 0
    responses: int = 0
    metadata: list[dict[str, float | int]] = field(default_factory=list)

    def response_hook(self, headers: Mapping[str, Any], body: Any) -> None:
        try:
            charge = float(headers.get("x-ms-request-charge", 0))
        except (TypeError, ValueError, OverflowError):
            return
        if not math.isfinite(charge) or charge < 0:
            return
        self.total_ru += min(charge, 1_000_000)
        self.responses += 1
        if len(self.metadata) < 100:
            self.metadata.append({"request_charge": charge})


_request_charge: ContextVar[RequestCharge | None] = ContextVar("request_charge", default=None)


@contextmanager
def track_request_charge():
    charge = RequestCharge()
    token = _request_charge.set(charge)
    try:
        yield charge
    finally:
        _request_charge.reset(token)


def response_hook(headers: Mapping[str, Any], body: Any) -> None:
    charge = _request_charge.get()
    if charge is not None:
        charge.response_hook(headers, body)


def _safe_error(error: AzureError) -> ApplicationError:
    status = getattr(error, "status_code", None)
    if isinstance(error, exceptions.CosmosBatchOperationError):
        responses = error.operation_responses or []
        index = error.error_index
        if isinstance(index, int) and 0 <= index < len(responses):
            status = responses[index].get("statusCode", status)
    if status in {409, 412}:
        return Conflict("The document changed or already exists.")
    if status in {401, 403}:
        return Forbidden("Azure access was denied.")
    if status == 400:
        return Invalid("Azure rejected the operation.")
    return Unavailable("The Azure operation is unavailable.")


class CosmosRepository:
    """Lazy sync clients for existing resources; callers offload to worker threads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.scope_id = settings.scope_id
        self._client: CosmosClient | None = None
        self._credential: DefaultAzureCredential | None = None
        self._operations: ContainerProxy | None = None
        self._evidence: ContainerProxy | None = None
        self._lock = RLock()

    @property
    def client(self) -> CosmosClient:
        self.settings.require_live("cosmos_endpoint", "cosmos_database")
        with self._lock:
            if self._client is None:
                credential: Any
                if self.settings.cosmos_auth_mode == "key":
                    credential = self.settings.cosmos_key.get_secret_value()
                    if not credential:
                        raise Unavailable("Explicit key authentication is not configured.")
                else:
                    self._credential = DefaultAzureCredential()
                    credential = self._credential
                try:
                    self._client = CosmosClient(
                        self.settings.cosmos_endpoint,
                        credential=credential,
                        connection_timeout=10,
                        timeout=30,
                        retry_total=3,
                        logging_enable=False,
                    )
                except (AzureError, ValueError):
                    if self._credential:
                        self._credential.close()
                        self._credential = None
                    raise Unavailable("The Cosmos client could not be initialized.") from None
            return self._client

    @property
    def operations(self) -> ContainerProxy:
        with self._lock:
            self.settings.require_live("cosmos_endpoint", "cosmos_operations_container")
            if self._operations is None:
                self._operations = self.client.get_database_client(
                    self.settings.cosmos_database
                ).get_container_client(self.settings.cosmos_operations_container)
            return self._operations

    @property
    def evidence(self) -> ContainerProxy:
        with self._lock:
            self.settings.require_live("cosmos_endpoint", "cosmos_evidence_container")
            if self._evidence is None:
                self._evidence = self.client.get_database_client(
                    self.settings.cosmos_database
                ).get_container_client(self.settings.cosmos_evidence_container)
            return self._evidence

    def _call(self, action: Callable[[], _T]) -> _T:
        self.settings.require_live("cosmos_endpoint", "cosmos_database")
        try:
            return action()
        except AzureError as error:
            raise _safe_error(error) from None

    def read(self, id: str) -> Document | None:
        validate_id(id)

        def point_read() -> Document | None:
            try:
                return self.operations.read_item(
                    item=id, partition_key=self.scope_id, response_hook=response_hook
                )
            except exceptions.CosmosResourceNotFoundError:
                return None

        document = self._call(point_read)
        return self._scoped(document) if document is not None else None

    def _scoped(self, document: Document) -> Document:
        if document.get("scope_id") != self.scope_id:
            raise Forbidden("The returned document is outside the configured scope.")
        return document

    def create(self, document: Document) -> Document:
        body = validate_document(document, self.scope_id)
        return self._scoped(
            self._call(lambda: self.operations.create_item(body=body, response_hook=response_hook))
        )

    def query(self, kind: str, *, filters: dict | None = None, limit: int = 50) -> list[Document]:
        filters = validate_query(kind, filters, limit)
        conditions = ["c.scope_id = @scope", "c.type = @kind"]
        parameters: list[dict[str, object]] = [
            {"name": "@scope", "value": self.scope_id},
            {"name": "@kind", "value": kind},
        ]
        for index, (key, value) in enumerate(sorted(filters.items())):
            conditions.append(f"c.{key} = @f{index}")
            parameters.append({"name": f"@f{index}", "value": value})
        sql = (
            f"SELECT TOP {limit} * FROM c WHERE "
            + " AND ".join(conditions)
            + " ORDER BY c._ts DESC"
        )
        documents = self._call(
            lambda: list(
                self.operations.query_items(
                    query=sql,
                    parameters=parameters,
                    partition_key=self.scope_id,
                    enable_cross_partition_query=False,
                    response_hook=response_hook,
                )
            )
        )
        return [self._scoped(doc) for doc in documents]

    def batch(self, operations: list[tuple]) -> list[Document]:
        checked = validate_batch(operations, self.scope_id)
        results = self._call(
            lambda: self.operations.execute_item_batch(
                batch_operations=checked,
                partition_key=self.scope_id,
                response_hook=response_hook,
            )
        )
        documents = []
        for result in results:
            body = result.get("resourceBody")
            if not isinstance(body, dict):
                raise Unavailable("Cosmos did not return the committed document.")
            documents.append(self._scoped(body))
        return documents

    def patch(self, id: str, operations: list[dict], etag: str) -> Document:
        validate_id(id)
        validate_patch(operations, etag)
        return self._scoped(
            self._call(
                lambda: self.operations.patch_item(
                    item=id,
                    partition_key=self.scope_id,
                    patch_operations=deepcopy(operations),
                    etag=etag,
                    match_condition=MatchConditions.IfNotModified,
                    response_hook=response_hook,
                )
            )
        )

    def close(self) -> None:
        with self._lock:
            if self._client:
                self._client.close()
            if self._credential:
                self._credential.close()
            self._client = None
            self._credential = None
            self._operations = None
            self._evidence = None
