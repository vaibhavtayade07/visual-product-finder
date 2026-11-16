"""FastAPI application factory: API + in-process workers + scheduler + static pages."""
from __future__ import annotations

import asyncio
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from .api import admin, health, search
from .context import AppContext, build_storage, load_defaults, mock_image_dir
from .db import Database
from .errors import AppError
from .migrations import run_migrations
from .providers import build_embedding_provider, build_vision_provider
from .settings import PROJECT_ROOT, Settings, get_settings
from .workers.pool import WorkerPool
from .workers.scheduler import Scheduler

log = logging.getLogger(__name__)


def _configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level, logging.INFO),
        format='{"ts":"%(asctime)s","level":"%(levelname)s","logger":"%(name)s","msg":"%(message)s"}',
    )


async def _seed_default_tenant(ctx: AppContext) -> None:
    await ctx.db.execute(
        """INSERT INTO tenants (slug, name, status, config, created_at)
           VALUES (%s, %s, 'active', '{}'::jsonb, now())
           ON CONFLICT (slug) DO NOTHING""",
        (ctx.settings.default_tenant_slug, "Demo Lighting Store"),
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    _configure_logging(settings.log_level)
    defaults = load_defaults()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if settings.run_migrations_on_start:
            await asyncio.to_thread(run_migrations, settings.database_url)

        db = Database(settings.database_url)
        await db.open()
        ctx = AppContext(
            settings=settings,
            db=db,
            storage=build_storage(settings),
            embed=build_embedding_provider(settings, defaults),
            vision=None,
            query_queue=asyncio.Queue(),
            defaults=defaults,
        )
        ctx.vision = build_vision_provider(settings, defaults)
        app.state.ctx = ctx

        await _seed_default_tenant(ctx)
        if settings.workers_enabled:
            ctx.worker_pool = WorkerPool(ctx)
            await ctx.worker_pool.start()
        if settings.scheduler_enabled:
            ctx.scheduler = Scheduler(ctx)
            await ctx.scheduler.start()
        log.info("app ready mode=%s embed=%s vision=%s", settings.app_mode, ctx.embed.model,
                 getattr(ctx.vision, "name", "none"))
        try:
            yield
        finally:
            if ctx.scheduler:
                await ctx.scheduler.stop()
            if ctx.worker_pool:
                await ctx.worker_pool.stop()
            await db.close()

    app = FastAPI(title="Visual Product Finder", version=health.VERSION, lifespan=lifespan)

    @app.middleware("http")
    async def request_id_middleware(request: Request, call_next):
        request_id = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        request.state.request_id = request_id
        start = time.monotonic()
        response = await call_next(request)
        response.headers["X-Request-ID"] = request_id
        if request.url.path.startswith("/api/"):
            log.info("request %s %s -> %s (%.0f ms) rid=%s", request.method, request.url.path,
                     response.status_code, (time.monotonic() - start) * 1000, request_id)
        return response

    @app.exception_handler(AppError)
    async def app_error_handler(request: Request, exc: AppError):
        rid = getattr(request.state, "request_id", "-")
        log.warning("app error %s %s: %s rid=%s", exc.status_code, exc.code, exc.message, rid)
        return JSONResponse(status_code=exc.status_code, content=exc.envelope())

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(request: Request, exc: RequestValidationError):
        return JSONResponse(status_code=400, content={
            "error": {"code": "BAD_REQUEST", "message": "Invalid request.",
                      "details": {"errors": exc.errors()}},
        })

    @app.exception_handler(Exception)
    async def unhandled_error_handler(request: Request, exc: Exception):
        rid = getattr(request.state, "request_id", "-")
        log.exception("unhandled error rid=%s", rid)
        return JSONResponse(status_code=500, content={
            "error": {"code": "INTERNAL", "message": "Something went wrong on our side.",
                      "details": {"request_id": rid}},
        })

    app.include_router(health.router)
    app.include_router(search.router)
    app.include_router(admin.router)

    static_dir = PROJECT_ROOT / "app" / "static"
    templates = Jinja2Templates(directory=str(PROJECT_ROOT / "app" / "templates"))
    app.mount("/static", StaticFiles(directory=static_dir), name="static")
    app.mount("/files", StaticFiles(directory=settings.data_dir / "storage", check_dir=False), name="files")
    app.mount("/mock-images", StaticFiles(directory=mock_image_dir(settings), check_dir=False), name="mock-images")

    @app.get("/")
    async def root():
        return RedirectResponse("/find")

    @app.get("/find")
    async def find_page(request: Request):
        ctx: AppContext = request.app.state.ctx
        return templates.TemplateResponse(request, "find.html", {
            "api_base": "/api/v1",
            "tenant_slug": settings.default_tenant_slug,
            "consent_version": "2026-10-v1",
            "retention_days": int(ctx.effective_config({}).get("retention_days", 30)),
        })

    @app.get("/admin")
    async def admin_page(request: Request):
        ctx: AppContext = request.app.state.ctx
        return templates.TemplateResponse(request, "admin.html", {
            "api_base": "/api/v1",
            "default_admin_token": settings.admin_token if settings.is_mock else "",
            "app_mode": settings.app_mode,
            "retention_days": int(ctx.effective_config({}).get("retention_days", 30)),
            "shopify_configured": bool(settings.shopify_shop_domain and settings.shopify_admin_token),
            "shopify_shop_domain": settings.shopify_shop_domain,
        })

    if settings.cors_origins:
        from fastapi.middleware.cors import CORSMiddleware  # noqa: PLC0415

        app.add_middleware(CORSMiddleware, allow_origins=settings.cors_origins, allow_methods=["*"],
                           allow_headers=["*"])

    return app


app = create_app()
