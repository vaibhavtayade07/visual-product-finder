"""FastAPI dependencies: context, tenant resolution, admin auth, rate limiting."""
from __future__ import annotations

import secrets

from fastapi import Request

from ..context import AppContext, Tenant
from ..errors import AppError, NotFound


class Unauthorized(AppError):
    status_code, code = 401, "UNAUTHORIZED"


def get_ctx(request: Request) -> AppContext:
    return request.app.state.ctx


async def resolve_tenant(request: Request, slug: str | None = None) -> Tenant:
    """Resolve a tenant by explicit slug, then `?tenant=` query param, then the default.

    Query-param fallback keeps admin routes multi-tenant without new routes (D2:
    every table/path is tenant-scoped; admin tooling targets one tenant at a time).
    """
    ctx = get_ctx(request)
    slug = slug or request.query_params.get("tenant") or ctx.settings.default_tenant_slug
    tenant = await ctx.get_tenant_by_slug(slug)
    if not tenant:
        raise NotFound("unknown tenant")
    return tenant


def require_admin(request: Request) -> None:
    ctx = get_ctx(request)
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        raise Unauthorized("Admin token required.")
    token = header.split(" ", 1)[1].strip()
    if not secrets.compare_digest(token, ctx.settings.admin_token):
        raise Unauthorized("Admin token required.")


def client_ip(request: Request) -> str:
    forwarded = request.headers.get("x-forwarded-for")
    if forwarded:
        return forwarded.split(",")[0].strip()
    return request.client.host if request.client else "unknown"
