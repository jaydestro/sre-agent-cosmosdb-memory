import base64
import json

import pytest
from starlette.requests import Request

from app.config import Settings
from app.errors import Forbidden
from app.security import Principal, authorize, get_principal, validate_origin


def request(*, client="127.0.0.1", host="localhost:8000", headers=None, scheme="http"):
    values = {"host": host, **(headers or {})}
    return Request(
        {
            "type": "http",
            "method": "POST",
            "path": "/",
            "scheme": scheme,
            "query_string": b"",
            "client": (client, 1234),
            "server": ("localhost", 8000),
            "headers": [(key.lower().encode(), value.encode()) for key, value in values.items()],
        }
    )


def proxy_header(oid="real-operator", tid="tenant", audience="audience", **kwargs):
    claims = [
        {"typ": "oid", "val": oid},
        {"typ": "tid", "val": tid},
        {"typ": "aud", "val": audience},
        {"typ": "roles", "val": "admin"},
    ]
    return base64.b64encode(
        json.dumps(
            {
                "auth_typ": "aad",
                "claims": claims,
                **kwargs,
            }
        ).encode()
    ).decode()


def deployed(**kwargs):
    return Settings(
        _env_file=None,
        app_env="production",
        trusted_auth_proxy=True,
        operator_tenant_id="tenant",
        operator_audience="audience",
        operator_memberships_json={
            "real-operator": {"roles": ["viewer"], "scopes": ["server-scope"]},
        },
        **kwargs,
    )


def test_local_identity_server_membership_and_role_hierarchy():
    settings = Settings(_env_file=None)
    admin = get_principal(request(), settings)
    assert admin.subject == "engineer-a"
    authorize(admin, "operator", settings.scope_id)
    operator = get_principal(request(headers={"X-Demo-Operator": "engineer-b"}), settings)
    authorize(operator, "viewer", settings.scope_id)
    with pytest.raises(Forbidden):
        authorize(operator, "admin", settings.scope_id)
    with pytest.raises(Forbidden):
        authorize(admin, "viewer", "other-scope")
    with pytest.raises(Forbidden):
        get_principal(request(headers={"X-Demo-Operator": "unknown"}), settings)


@pytest.mark.parametrize(
    "client,host,app_host",
    [
        ("10.0.0.1", "localhost:8000", "127.0.0.1"),
        ("127.0.0.1", "public.example:8000", "127.0.0.1"),
        ("127.0.0.1", "localhost:8000", "0.0.0.0"),
        ("127.0.0.1", "localhost.evil:8000", "127.0.0.1"),
        ("127.0.0.1", "[bad", "127.0.0.1"),
    ],
)
def test_local_impersonation_requires_all_loopback_conditions(client, host, app_host):
    with pytest.raises(Forbidden):
        get_principal(
            request(client=client, host=host), Settings(_env_file=None, app_host=app_host)
        )


def test_deployed_never_accepts_demo_header_and_requires_proxy():
    with pytest.raises(Forbidden):
        get_principal(request(headers={"X-Demo-Operator": "engineer-a"}), deployed())
    with pytest.raises(Forbidden):
        get_principal(
            request(headers={"X-MS-CLIENT-PRINCIPAL": proxy_header()}),
            Settings(_env_file=None, app_env="production"),
        )


def test_proxy_uses_server_owned_roles_and_scopes_not_claims():
    principal = get_principal(
        request(
            headers={
                "X-MS-CLIENT-PRINCIPAL": proxy_header(),
                "X-Demo-Operator": "engineer-a",
            }
        ),
        deployed(),
    )
    assert principal.subject == "real-operator"
    assert principal.roles == frozenset({"viewer"})
    assert principal.scopes == frozenset({"server-scope"})
    with pytest.raises(Forbidden):
        authorize(principal, "admin", "server-scope")


@pytest.mark.parametrize(
    "header",
    [
        "garbage",
        proxy_header(tid="foreign"),
        proxy_header(audience="foreign"),
        proxy_header(oid="not-a-member"),
        proxy_header(auth_typ="anonymous"),
        proxy_header(claims=[{"typ": "oid", "val": "real-operator"}]),
        proxy_header(
            claims=[{"typ": "oid", "val": "real-operator"}, {"typ": "oid", "val": "second"}]
        ),
    ],
)
def test_invalid_proxy_claims_fail_closed(header):
    with pytest.raises(Forbidden):
        get_principal(request(headers={"X-MS-CLIENT-PRINCIPAL": header}), deployed())


def test_empty_unknown_roles_and_scopes_are_rejected():
    identity = Principal("service", frozenset({"unknown"}), frozenset({"scope"}), kind="service")
    with pytest.raises(Forbidden):
        authorize(identity, "viewer", "scope")


@pytest.mark.parametrize(
    "origin",
    [
        None,
        "null",
        "http://evil.example:8000",
        "https://localhost:8000",
        "http://localhost:8001",
        "http://localhost:8000@evil.example",
        "http://localhost:8000/path",
        "http://localhost:8000?query",
        "http://[broken",
    ],
)
def test_mutations_require_same_origin(origin):
    headers = {"X-Requested-With": "IncidentMemory"}
    if origin is not None:
        headers["Origin"] = origin
    with pytest.raises(Forbidden):
        validate_origin(request(headers=headers), Settings(_env_file=None))


def test_same_origin_requires_application_csrf_header():
    settings = Settings(_env_file=None)
    with pytest.raises(Forbidden):
        validate_origin(request(headers={"Origin": "http://localhost:8000"}), settings)
    validate_origin(
        request(
            headers={
                "Origin": "http://localhost:8000",
                "X-Requested-With": "IncidentMemory",
            }
        ),
        settings,
    )
