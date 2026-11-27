"""HTTP boundary for the governed incident console; no provisioning at startup."""

import asyncio
import inspect
import logging
import time
from contextlib import asynccontextmanager
from importlib.resources import files
from typing import Annotated, Literal
from uuid import uuid4

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.agent import UnsupportedQuestion, validate_question
from app.config import Settings
from app.errors import ApplicationError, Conflict, Unavailable
from app.security import authorize, get_principal, validate_origin

LOG = logging.getLogger(__name__)
Identifier = Annotated[str, StringConstraints(min_length=1, max_length=160, pattern=r"^[\w:.-]+$")]


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class Investigation(Input):
    question: Annotated[
        str, StringConstraints(strip_whitespace=True, min_length=1, max_length=2000)
    ]
    incident_id: Identifier | None = None
    memory_enabled: bool = True


class PolicyInput(Input):
    enabled: bool = True
    allow_rollback: bool = False
    expires_at: Annotated[str, StringConstraints(min_length=20, max_length=40)]
    max_attempts: int = Field(default=3, ge=1, le=3)
    max_actions: int = Field(default=10, ge=1, le=10)
    cooldown_seconds: int = Field(default=30, ge=30, le=3600)
    approval_expiration_seconds: int = Field(default=300, ge=1, le=300)


class ModeInput(Input):
    mode: Literal["guarded", "autonomous", "paused"]
    expected_version: int = Field(ge=1)
    policy: PolicyInput | None = None


class Approval(Input):
    digest: Annotated[str, StringConstraints(min_length=64, max_length=64, pattern=r"^[0-9a-f]+$")]


class Review(Input):
    reason: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=1000)]


class Scenario(Input):
    scenario: Literal["pool_exhaustion", "deployment_error", "healthy"]


async def invoke(method, *args, **kwargs):
    """Keep synchronous repositories/SDKs off the event loop."""
    if inspect.iscoroutinefunction(method):
        return await method(*args, **kwargs)
    value = await asyncio.to_thread(method, *args, **kwargs)
    return await value if inspect.isawaitable(value) else value


def create_app(settings: Settings | None = None, dependencies: dict | None = None) -> FastAPI:
    config = settings if settings is not None else Settings()
    supplied = dict(dependencies) if dependencies is not None else None
    services = supplied or {}
    snapshot_cache: dict = {}
    snapshot_lock = asyncio.Lock()

    def require(name):
        if not app.state.ready or name not in services:
            raise Unavailable()
        return services[name]

    async def principal(request, role="viewer", mutation=False):
        identity = get_principal(request, config)
        authorize(identity, role, config.scope_id)
        if mutation:
            validate_origin(request, config)
        return identity

    def invalidate_cache():
        snapshot_cache.clear()

    @asynccontextmanager
    async def lifespan(application):
        task = None
        application.state.ready = False
        application.state.setup_reason = (
            "Azure use is disabled. Enable it only after developer setup."
        )
        try:
            if supplied is not None:
                application.state.ready = "repo" in services
            elif config.allow_azure_use:
                config.require_live(
                    "cosmos_endpoint",
                    "foundry_endpoint",
                    "reasoning_deployment",
                    "memory_deployment",
                    "memory_embedding_deployment",
                    "blob_account_url",
                    "blob_container",
                    "embedding_endpoint",
                )
                from app.agent import AgentService
                from app.cosmos import CosmosRepository
                from app.evidence import EvidenceService
                from app.memory import MemoryService
                from app.monitoring import Monitor
                from app.remediation import Governance
                from app.simulation import Simulation

                services["repo"] = await asyncio.to_thread(CosmosRepository, config)
                repo = services["repo"]
                services["simulation"] = Simulation(repo, config)
                services["governance"] = Governance(repo, config)
                services["evidence"] = EvidenceService(repo, config)
                services["memory"] = MemoryService(repo, config)
                services["agent"] = AgentService(
                    repo,
                    config,
                    evidence=services["evidence"],
                    memory=services["memory"],
                    governance=services["governance"],
                )
                services["monitor"] = Monitor(
                    repo, config, services["governance"], services["agent"], services["memory"]
                )
                # Existence checks only: startup must never seed state or create resources.
                if not await invoke(repo.read, f"service:{config.service_id}"):
                    raise Unavailable()
                if not await invoke(repo.read, "control"):
                    raise Unavailable()
                application.state.ready = True
            if application.state.ready and config.allow_azure_use and "monitor" in services:
                task = asyncio.create_task(services["monitor"].run(), name="incident-monitor")
                application.state.monitor_task = task
                task.add_done_callback(
                    lambda completed: (
                        LOG.warning("Incident monitor stopped")
                        if not completed.cancelled() and completed.exception()
                        else None
                    )
                )
        except Exception:
            application.state.ready = False
            application.state.setup_reason = (
                "Live services are unavailable. Check configured endpoints, access, "
                "preprovisioned resources, and the explicit development seed."
            )
            LOG.warning("Live incident services could not be initialized")
        try:
            yield
        finally:
            if task is not None:
                try:
                    await invoke(services["monitor"].stop)
                finally:
                    task.cancel()
                    await asyncio.gather(task, return_exceptions=True)
            closed = set()
            for service in reversed(list(services.values())):
                if id(service) in closed:
                    continue
                closed.add(id(service))
                close = getattr(service, "close", None)
                if close:
                    try:
                        await invoke(close)
                    except Exception:
                        LOG.warning("Incident service close failed")

    app = FastAPI(title="Incident Memory", lifespan=lifespan, docs_url=None, redoc_url=None)
    app.state.ready = False
    app.state.settings = config
    app.state.services = services

    @app.middleware("http")
    async def response_security(request, call_next):
        try:
            response = await call_next(request)
        except Exception as error:
            LOG.warning("Incident request failed (%s)", type(error).__name__)
            response = JSONResponse(
                {"error": "The operation is temporarily unavailable."}, status_code=503
            )
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = (
            "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' blob:; "
            "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        )
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(ApplicationError)
    async def domain_error(request, error):
        if isinstance(error, UnsupportedQuestion):
            return JSONResponse({"error": str(error)}, status_code=422)
        code = error.status_code if error.status_code in {403, 409, 422, 503} else 422
        messages = {
            403: "This operation is not authorized.",
            409: "State changed, approval expired, or the exact action no longer matches. Refresh.",
            422: "The request is not valid.",
            503: "Live services are unavailable. Complete developer setup before use.",
        }
        return JSONResponse({"error": messages[code]}, status_code=code)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request, error):
        return JSONResponse({"error": "The request is not valid."}, status_code=422)

    @app.exception_handler(Exception)
    async def unexpected_error(request, error):
        LOG.warning("Incident request failed (%s)", type(error).__name__)
        return JSONResponse({"error": "The operation is temporarily unavailable."}, status_code=503)

    @app.get("/")
    async def index():
        return Response(
            files("app").joinpath("web", "index.html").read_bytes(), media_type="text/html"
        )

    @app.get("/health/live")
    async def health():
        return {"status": "alive"}

    @app.get("/api/status")
    async def status(request: Request):
        identity = await principal(request)
        if not app.state.ready:
            return JSONResponse(
                {
                    "available": False,
                    "setup": getattr(app.state, "setup_reason", "Developer setup is required."),
                    "azure_use_enabled": config.allow_azure_use,
                    "operator": {"subject": identity.subject, "roles": sorted(identity.roles)},
                },
                status_code=503,
            )
        async with snapshot_lock:
            if (
                "value" not in snapshot_cache
                or time.monotonic() - snapshot_cache.get("at", 0) >= 10
            ):
                repo = require("repo")
                values = await asyncio.gather(
                    invoke(require("simulation").snapshot),
                    invoke(require("governance").control),
                    invoke(repo.query, "remediation", limit=20),
                    invoke(repo.query, "audit", limit=20),
                    invoke(repo.query, "incident", limit=20),
                    invoke(repo.query, "memory_review", limit=20),
                )
                snapshot_cache.update(
                    at=time.monotonic(),
                    value=dict(
                        zip(
                            (
                                "live",
                                "control",
                                "proposals",
                                "history",
                                "incidents",
                                "memory_reviews",
                            ),
                            values,
                            strict=True,
                        )
                    ),
                )
        monitor_task = getattr(app.state, "monitor_task", None)
        return {
            "available": True,
            **snapshot_cache["value"],
            "operator": {"subject": identity.subject, "roles": sorted(identity.roles)},
            "scope": config.scope_id,
            "service": config.service_id,
            "development": config.app_env == "development",
            "scenario_enabled": config.app_env == "development" and config.allow_demo_reset,
            "worker": {
                "running": monitor_task is not None and not monitor_task.done(),
                "interval_seconds": config.monitor_interval_seconds,
                "browser_required": False,
            },
            "snapshot_cache_seconds": 10,
        }

    @app.get("/api/evidence/{evidence_id}/image")
    async def evidence_image(evidence_id: Identifier, request: Request):
        identity = await principal(request)
        content, content_type = await invoke(require("evidence").image_bytes, evidence_id, identity)
        if content_type not in {"image/png", "image/jpeg", "image/webp"}:
            raise Unavailable()
        return Response(
            content, media_type=content_type, headers={"Cache-Control": "private, no-store"}
        )

    @app.post("/api/investigate")
    async def investigate(body: Investigation, request: Request):
        identity = await principal(request, "operator", True)
        validate_question(body.question)
        incident_id = body.incident_id
        repo = require("repo")
        if incident_id is None:
            live = await invoke(require("simulation").snapshot)
            incident_id = f"incident:{uuid4()}"
            await invoke(
                repo.create,
                {
                    "id": incident_id,
                    "type": "incident",
                    "scope_id": config.scope_id,
                    "service_id": config.service_id,
                    "status": "open",
                    "question": body.question,
                    "metrics": live["metrics"],
                    "root_cause": live.get("root_cause"),
                    "created_at": live.get("observed_at"),
                },
            )
        else:
            incident = await invoke(repo.read, incident_id)
            if not incident or incident.get("type") != "incident":
                raise Unavailable()
            if incident.get("scope_id") != config.scope_id:
                authorize(identity, "operator", incident.get("scope_id", ""))
                raise Unavailable()
        result = await invoke(
            require("agent").analyze,
            incident_id,
            identity,
            question=body.question,
            memory_enabled=body.memory_enabled,
        )
        invalidate_cache()
        return result

    @app.get("/api/memories")
    async def memories(request: Request):
        identity = await principal(request)
        return await invoke(require("memory").list, identity)

    @app.post("/api/memories/{memory_id}/review")
    async def review_memory(memory_id: Identifier, body: Review, request: Request):
        identity = await principal(request, "operator", True)
        result = await invoke(require("memory").review, memory_id, identity, body.reason)
        invalidate_cache()
        return result

    @app.post("/api/memories/{memory_id}/invalidate")
    async def invalidate_memory(memory_id: Identifier, body: Review, request: Request):
        identity = await principal(request, "operator", True)
        result = await invoke(require("memory").invalidate, memory_id, identity, body.reason)
        invalidate_cache()
        return result

    @app.post("/api/mode")
    async def mode(body: ModeInput, request: Request):
        identity = await principal(request, "admin", True)
        governance = require("governance")
        policy = None
        if body.policy is not None:
            current = await invoke(governance.control)
            policy = {**current["policy"], **body.policy.model_dump(exclude_unset=True)}
        result = await invoke(
            governance.set_mode, body.mode, identity, body.expected_version, policy=policy
        )
        invalidate_cache()
        return result

    @app.post("/api/proposals/{proposal_id}/approve")
    async def approve(proposal_id: Identifier, body: Approval, request: Request):
        identity = await principal(request, "operator", True)
        result = await invoke(require("governance").approve, proposal_id, body.digest, identity)
        invalidate_cache()
        if result.get("status") != "queued":
            raise Conflict()
        return result

    @app.post("/api/proposals/{proposal_id}/reject")
    async def reject(proposal_id: Identifier, body: Approval, request: Request):
        identity = await principal(request, "operator", True)
        result = await invoke(require("governance").reject, proposal_id, body.digest, identity)
        invalidate_cache()
        return result

    @app.post("/api/scenario")
    async def scenario(body: Scenario, request: Request):
        identity = await principal(request, "operator", True)
        if config.app_env != "development" or not config.allow_demo_reset:
            authorize(identity, "admin", "disabled-development-reset")
        result = await invoke(require("simulation").inject, body.scenario, identity)
        invalidate_cache()
        return result

    app.mount("/static", StaticFiles(packages=[("app", "web")]), name="static")
    return app


app = create_app()
