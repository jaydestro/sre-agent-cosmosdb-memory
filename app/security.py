import base64
import ipaddress
import json
from dataclasses import dataclass
from urllib.parse import urlsplit

from starlette.requests import Request

from app.config import Settings
from app.errors import Forbidden

_GRANTS = {
    "viewer": frozenset({"viewer"}),
    "operator": frozenset({"operator", "viewer"}),
    "admin": frozenset({"admin", "operator", "viewer"}),
}
_OID = ("oid", "http://schemas.microsoft.com/identity/claims/objectidentifier")
_TID = ("tid", "http://schemas.microsoft.com/identity/claims/tenantid")


@dataclass(frozen=True)
class Principal:
    subject: str
    roles: frozenset[str]
    scopes: frozenset[str]
    kind: str = "human"

    def __post_init__(self) -> None:
        object.__setattr__(self, "roles", frozenset(self.roles))
        object.__setattr__(self, "scopes", frozenset(self.scopes))


def authorize(principal: Principal, role: str, scope: str) -> None:
    grants = set().union(*(_GRANTS.get(r, frozenset()) for r in principal.roles))
    if role not in _GRANTS or role not in grants or scope not in principal.scopes:
        raise Forbidden("This identity is not authorized for the requested scope.")


def _loopback(host: str | None) -> bool:
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def _membership(subject: str, settings: Settings, *, local: bool = False) -> Principal:
    entry = settings.operator_memberships_json.get(subject)
    if entry is None and local and subject in settings.local_admin_ids:
        entry = {"roles": ["admin"], "scopes": [settings.scope_id]}
    if entry is None and local and subject == "engineer-b":
        entry = {"roles": ["operator"], "scopes": [settings.scope_id]}
    if not entry or not entry.get("roles") or not entry.get("scopes"):
        raise Forbidden("No operator membership is configured for this identity.")
    roles = frozenset(entry["roles"])
    if not roles <= _GRANTS.keys():
        raise Forbidden("Operator membership configuration is invalid.")
    return Principal(subject, roles, frozenset(entry["scopes"]))


def get_principal(request: Request, settings: Settings) -> Principal:
    """Trust proxy headers ONLY behind an ingress that strips and verifies them.

    Deployed Container Apps must enforce Entra authentication and prohibit direct
    ingress to the application. Never enable trusted_auth_proxy for public app
    ports: the encoded principal is not itself a signed credential.
    """
    if settings.app_env == "development":
        try:
            hostname = urlsplit("//" + request.headers.get("host", "")).hostname
        except ValueError:
            hostname = None
        if (
            not _loopback(settings.app_host)
            or not request.client
            or not _loopback(request.client.host)
            or not _loopback(hostname)
        ):
            raise Forbidden("Local identities are restricted to loopback development.")
        subject = request.headers.get("x-demo-operator", settings.local_operator_id)
        return _membership(subject, settings, local=True)
    if not settings.trusted_auth_proxy:
        raise Forbidden("A verified authentication proxy is required.")
    if not settings.operator_tenant_id or not settings.operator_audience:
        raise Forbidden("Operator authentication is not configured.")
    encoded = request.headers.get("x-ms-client-principal", "")
    if not encoded or len(encoded) > 16384:
        raise Forbidden("A verified operator identity is required.")
    try:
        payload = json.loads(base64.b64decode(encoded, validate=True))
        if payload.get("auth_typ", "").lower() not in {"aad", "entra", "azureactivedirectory"}:
            raise ValueError
        claims = payload["claims"]
        if not isinstance(claims, list) or len(claims) > 128:
            raise ValueError

        def claim(names: tuple[str, ...]) -> str:
            values = [c["val"] for c in claims if c.get("typ") in names]
            if len(values) != 1 or not isinstance(values[0], str):
                raise ValueError
            return values[0]

        subject = claim(_OID)
        if (
            claim(_TID) != settings.operator_tenant_id
            or claim(("aud",)) != settings.operator_audience
            or not subject
        ):
            raise ValueError
    except (ValueError, KeyError, TypeError, AttributeError):
        raise Forbidden("The operator identity is invalid.") from None
    return _membership(subject, settings)


def validate_origin(request: Request, settings: Settings) -> None:
    """Browser mutations require same-origin plus the application's CSRF header."""
    origin = request.headers.get("origin", "")
    try:
        parsed = urlsplit(origin)
        host = urlsplit(f"{request.url.scheme}://{request.headers.get('host', '')}")
        valid = (
            parsed.scheme in {"http", "https"}
            and not parsed.username
            and not parsed.password
            and parsed.path in {"", "/"}
            and not parsed.query
            and not parsed.fragment
            and parsed.scheme == host.scheme
            and parsed.hostname == host.hostname
            and (parsed.port or (443 if parsed.scheme == "https" else 80))
            == (host.port or (443 if host.scheme == "https" else 80))
        )
    except ValueError:
        valid = False
    if not valid or request.headers.get("x-requested-with") != "IncidentMemory":
        raise Forbidden("A same-origin application request is required.")
