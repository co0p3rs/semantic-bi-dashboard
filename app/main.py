"""App factory. Each call builds isolated state, so tests never share a warehouse."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import FastAPI, Request, Response
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import router as api_router
from app.auth import AuthService
from app.authz import Resource, Role
from app.cache import GenerationCache
from app.config import Settings, get_settings
from app.engine import Engine
from app.external_api import ApiKeyStore
from app.external_api import router as external_router
from app.warehouse import Warehouse

logging.basicConfig(level=logging.INFO)
STATIC = Path(__file__).parent / "static"

DEMO_USERS = (
    ("owner@northstar.example", Role.ADMIN),
    ("marketer@northstar.example", Role.MARKETER),
    ("viewer@northstar.example", Role.VIEWER),
)
# Local-only demo keys. Production keys are generated and shown once.
DEMO_API_KEYS = (
    ("finance-export", "demo-finance-key", frozenset({Resource.OVERVIEW})),
    ("agency-reporting", "demo-agency-key", frozenset({Resource.OVERVIEW, Resource.MARKETING})),
)

SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "X-Frame-Options": "DENY",
}


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    app = FastAPI(
        title="Northstar BI",
        description="Semantic-layer BI dashboard over a synthetic warehouse.",
        docs_url="/docs" if settings.docs_enabled else None,
        redoc_url=None,
        openapi_url="/openapi.json" if settings.docs_enabled else None,
    )

    warehouse = Warehouse(settings.warehouse_path, seed=settings.seed)
    cache = GenerationCache(ttl_seconds=settings.cache_ttl_seconds, max_entries=settings.cache_max_entries)
    auth = AuthService(
        idle_seconds=settings.session_idle_seconds,
        absolute_seconds=settings.session_absolute_seconds,
        max_failures=settings.login_max_failures,
        lockout_seconds=settings.login_lockout_seconds,
    )
    api_keys = ApiKeyStore(rate_per_minute=settings.external_rate_limit_per_minute)
    if settings.environment != "production":
        for email, role in DEMO_USERS:
            auth.create_user(email, role, settings.demo_password.get_secret_value())
        for name, raw, resources in DEMO_API_KEYS:
            api_keys.add(name, raw, resources)

    app.state.settings = settings
    app.state.engine = Engine(warehouse, cache)
    app.state.auth = auth
    app.state.api_keys = api_keys

    @app.middleware("http")
    async def security_headers(request: Request, call_next: Any) -> Response:
        response: Response = await call_next(request)
        for name, value in SECURITY_HEADERS.items():
            response.headers.setdefault(name, value)
        if request.url.path.startswith(("/api", "/v1")):
            response.headers["Cache-Control"] = "no-store"
        return response

    @app.get("/healthz", tags=["health"])
    def healthz() -> dict[str, Any]:
        state = warehouse.sync_state()
        return {
            "status": "ok",
            "sources": {k: v["synced_through"] for k, v in state.items()},
            "cache": {"entries": len(cache), "hits": cache.stats.hits, "misses": cache.stats.misses},
        }

    app.include_router(api_router)
    app.include_router(external_router)
    app.mount("/static", StaticFiles(directory=STATIC), name="static")

    @app.get("/", include_in_schema=False)
    def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    return app


app = create_app()
