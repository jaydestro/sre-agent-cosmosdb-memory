"""Deterministic test-only repository. Never import this module in application code."""

from copy import deepcopy
from threading import RLock
from typing import Any

from app.cosmos import (
    validate_batch,
    validate_document,
    validate_id,
    validate_patch,
    validate_query,
)
from app.errors import Conflict, Invalid


class InMemoryRepository:
    def __init__(self, scope_id: str = "demo-payments-development") -> None:
        self.scope_id = scope_id
        self._documents: dict[str, dict[str, Any]] = {}
        self._version = 0
        self._lock = RLock()

    def _write(self, document: dict) -> dict:
        self._version += 1
        body = deepcopy(document)
        body["_etag"] = f'"{self._version}"'
        self._documents.pop(body["id"], None)
        self._documents[body["id"]] = body
        return deepcopy(body)

    def read(self, id: str) -> dict | None:
        validate_id(id)
        with self._lock:
            return deepcopy(self._documents.get(id))

    def create(self, document: dict) -> dict:
        body = validate_document(document, self.scope_id)
        with self._lock:
            if body["id"] in self._documents:
                raise Conflict("The document already exists.")
            return self._write(body)

    def query(self, kind: str, *, filters: dict | None = None, limit: int = 50) -> list[dict]:
        filters = validate_query(kind, filters, limit)
        with self._lock:
            return deepcopy(
                [
                    doc
                    for doc in reversed(self._documents.values())
                    if doc["scope_id"] == self.scope_id
                    and doc["type"] == kind
                    and all(doc.get(key) == value for key, value in filters.items())
                ][:limit]
            )

    def _replace(self, id: str, body: dict, etag: str) -> dict:
        current = self._documents.get(id)
        if not current or current["_etag"] != etag:
            raise Conflict("The document changed or is missing.")
        return self._write(body)

    def patch(self, id: str, operations: list[dict], etag: str) -> dict:
        validate_id(id)
        validate_patch(operations, etag)
        operations = deepcopy(operations)
        with self._lock:
            current = self._documents.get(id)
            if not current or current["_etag"] != etag:
                raise Conflict("The document changed or is missing.")
            body = deepcopy(current)
            try:
                for operation in operations:
                    parts = [
                        p.replace("~1", "/").replace("~0", "~")
                        for p in operation["path"][1:].split("/")
                    ]
                    parent: Any = body
                    for part in parts[:-1]:
                        parent = parent[int(part)] if isinstance(parent, list) else parent[part]
                    key: Any = parts[-1]
                    if isinstance(parent, list):
                        if key == "-" and operation["op"] == "add":
                            parent.append(deepcopy(operation["value"]))
                            continue
                        key = int(key)
                        if (
                            key < 0
                            or key > len(parent)
                            or (key == len(parent) and operation["op"] != "add")
                        ):
                            raise Invalid("The patch path is missing.")
                    op = operation["op"]
                    if op == "remove":
                        del parent[key]
                    elif op == "incr":
                        value = operation["value"]
                        if isinstance(value, bool) or not isinstance(value, (int, float)):
                            raise Invalid("A numeric increment is required.")
                        current_value = (
                            parent.get(key, 0) if isinstance(parent, dict) else parent[key]
                        )
                        if isinstance(current_value, bool) or not isinstance(
                            current_value, (int, float)
                        ):
                            raise Invalid("The increment target must be numeric.")
                        parent[key] = (
                            parent.get(key, 0) + value
                            if isinstance(parent, dict)
                            else (parent[key] + value)
                        )
                    elif op == "replace":
                        if isinstance(parent, dict) and key not in parent:
                            raise Invalid("The patch path is missing.")
                        parent[key] = deepcopy(operation["value"])
                    elif op == "add" and isinstance(parent, list):
                        parent.insert(key, deepcopy(operation["value"]))
                    else:
                        parent[key] = deepcopy(operation["value"])
            except (KeyError, IndexError, TypeError, ValueError):
                raise Invalid("The patch path or value is invalid.") from None
            return self._write(validate_document(body, self.scope_id))

    def batch(self, operations: list[tuple]) -> list[dict]:
        checked = validate_batch(operations, self.scope_id)
        with self._lock:
            before, version = deepcopy(self._documents), self._version
            try:
                results = []
                for operation in checked:
                    name, args = operation[:2]
                    kwargs = operation[2] if len(operation) == 3 else {}
                    if name == "create":
                        results.append(self.create(args[0]))
                    elif name == "replace":
                        results.append(self._replace(*args, kwargs["if_match_etag"]))
                    else:
                        results.append(self.patch(*args, kwargs["if_match_etag"]))
                return results
            except Exception:
                self._documents, self._version = before, version
                raise

    def close(self) -> None:
        pass
