"""Dashboard routes: session auth, permission-filtered catalog, queries, admin."""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any

from fastapi import APIRouter, Cookie, Depends, Header, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.auth import AuthError, AuthService, Session, User, csrf_ok
from app.authz import Capability, Forbidden, Role, can_read_metric, require_metrics
from app.engine import Engine
from app.semantic import (
    ContractError,
    DIMENSION_VALUES,
    Query,
    catalog,
    compare,
    parse_query,
    previous_period,
)

SESSION_COOKIE = "nbi_session"
CSRF_COOKIE = "nbi_csrf"

router = APIRouter(prefix="/api", tags=["dashboard"])

OVERVIEW_TILES = (
    "net_revenue",
    "orders",
    "average_order_value",
    "new_customers",
    "mer",
    "cac",
    "conversion_rate",
    "refund_rate",
)


# --- dependencies --------------------------------------------------------


def _auth(request: Request) -> AuthService:
    return request.app.state.auth


def _engine(request: Request) -> Engine:
    return request.app.state.engine


def current(
    request: Request,
    nbi_session: str | None = Cookie(default=None),
) -> tuple[User, Session]:
    try:
        return _auth(request).resolve(nbi_session)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc


def mutating(
    who: tuple[User, Session] = Depends(current),
    x_csrf_token: str | None = Header(default=None),
) -> tuple[User, Session]:
    if not csrf_ok(who[1], x_csrf_token):
        raise HTTPException(status.HTTP_403_FORBIDDEN, "csrf token missing or wrong")
    return who


def admin(who: tuple[User, Session] = Depends(mutating)) -> User:
    if Capability.USERS_MANAGE not in who[0].capabilities:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return who[0]


def _contract(fn: Any, **kwargs: Any) -> Query:
    try:
        return fn(**kwargs)
    except ContractError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc


def _allow(user: User, metrics: tuple[str, ...]) -> None:
    try:
        require_metrics(user.resources, metrics)
    except Forbidden as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc


# --- auth ----------------------------------------------------------------


class LoginBody(BaseModel):
    email: str = Field(max_length=254)
    password: str = Field(max_length=256)


class ActivateBody(BaseModel):
    token: str = Field(max_length=128)
    password: str = Field(max_length=256)


def _set_session(request: Request, response: Response, token: str, session: Session) -> None:
    secure = request.app.state.settings.cookie_secure
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax", secure=secure, path="/")
    # Readable by the page so it can echo it in X-CSRF-Token.
    response.set_cookie(CSRF_COOKIE, session.csrf_token, httponly=False, samesite="lax", secure=secure, path="/")


@router.post("/auth/login")
def login(body: LoginBody, request: Request, response: Response) -> dict[str, Any]:
    try:
        token, session = _auth(request).login(body.email, body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, str(exc)) from exc
    _set_session(request, response, token, session)
    return {"ok": True}


@router.post("/auth/logout")
def logout(
    request: Request,
    response: Response,
    _: tuple[User, Session] = Depends(mutating),
    nbi_session: str | None = Cookie(default=None),
) -> dict[str, Any]:
    _auth(request).logout(nbi_session)
    response.delete_cookie(SESSION_COOKIE, path="/")
    response.delete_cookie(CSRF_COOKIE, path="/")
    return {"ok": True}


@router.post("/auth/activate")
def activate(body: ActivateBody, request: Request) -> dict[str, Any]:
    try:
        user = _auth(request).activate(body.token, body.password)
    except AuthError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    return {"ok": True, "email": user.email}


@router.get("/me")
def me(who: tuple[User, Session] = Depends(current)) -> dict[str, Any]:
    user = who[0]
    return {
        "email": user.email,
        "role": user.role.value,
        "resources": sorted(r.value for r in user.resources),
        "capabilities": sorted(c.value for c in user.capabilities),
    }


# --- data ----------------------------------------------------------------


@router.get("/v1/catalog")
def get_catalog(who: tuple[User, Session] = Depends(current)) -> dict[str, Any]:
    resources = who[0].resources
    return {
        "metrics": [item for item in catalog() if can_read_metric(resources, str(item["metric"]))],
        "dimensions": {d.value: sorted(v) for d, v in DIMENSION_VALUES.items()},
    }


class QueryBody(BaseModel):
    metrics: list[str]
    start_date: str
    end_date: str
    grain: str = "day"
    group_by: list[str] = Field(default_factory=list)
    filters: dict[str, list[str]] = Field(default_factory=dict)


@router.post("/v1/query")
def query(
    body: QueryBody,
    request: Request,
    who: tuple[User, Session] = Depends(current),
) -> dict[str, Any]:
    parsed = _contract(parse_query, **body.model_dump())
    _allow(who[0], parsed.metrics)
    result, meta = _engine(request).execute(parsed)
    return {**result, "meta": meta}


class CompareBody(QueryBody):
    compare: str = "previous_period"


@router.post("/v1/comparison")
def comparison(
    body: CompareBody,
    request: Request,
    who: tuple[User, Session] = Depends(current),
) -> dict[str, Any]:
    data = body.model_dump()
    mode = data.pop("compare")
    data["grain"] = "total"
    parsed = _contract(parse_query, **data)
    _allow(who[0], parsed.metrics)
    prior = _contract(previous_period, query=parsed, mode=mode)
    now_result, now_meta = _engine(request).execute(parsed)
    prior_result, _ = _engine(request).execute(prior)
    return {
        "compare": mode,
        "previous_range": [prior.start.isoformat(), prior.end.isoformat()],
        "metrics": compare(now_result["totals"], prior_result["totals"]),
        "meta": now_meta,
    }


class OverviewBody(BaseModel):
    start_date: str
    end_date: str


@router.post("/v1/dashboard/overview")
def overview(
    body: OverviewBody,
    request: Request,
    who: tuple[User, Session] = Depends(current),
) -> dict[str, Any]:
    """One round-trip for the landing page, shaped by what the user may see."""
    user = who[0]
    tiles = tuple(m for m in OVERVIEW_TILES if can_read_metric(user.resources, m))
    if not tiles:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "no overview access")
    engine = _engine(request)
    span = {"start_date": body.start_date, "end_date": body.end_date}

    totals_q = _contract(parse_query, metrics=list(tiles), grain="total", **span)
    prior_q = previous_period(totals_q, "previous_period")
    totals, meta = engine.execute(totals_q)
    prior, _ = engine.execute(prior_q)

    series_metrics = [m for m in ("net_revenue", "ad_spend") if can_read_metric(user.resources, m)]
    series, _ = engine.execute(_contract(parse_query, metrics=series_metrics, grain="day", **span))

    product_metrics = [m for m in ("net_revenue", "orders", "refund_rate", "mer") if m in tiles]
    by_product, _ = engine.execute(
        _contract(parse_query, metrics=product_metrics, grain="total", group_by=["product"], **span)
    )
    return {
        "tiles": compare(totals["totals"], prior["totals"]),
        "series": series["rows"],
        "series_metrics": series_metrics,
        "by_product": sorted(by_product["rows"], key=lambda r: -(r.get("net_revenue") or 0)),
        "previous_range": [prior_q.start.isoformat(), prior_q.end.isoformat()],
        "meta": meta,
    }


# --- admin ---------------------------------------------------------------


class InviteBody(BaseModel):
    email: str = Field(max_length=254)
    role: Role


class RevokeBody(BaseModel):
    email: str = Field(max_length=254)


class SyncBody(BaseModel):
    source: str
    synced_through: date


@router.get("/admin/users")
def list_users(request: Request, who: tuple[User, Session] = Depends(current)) -> dict[str, Any]:
    if Capability.USERS_MANAGE not in who[0].capabilities:
        raise HTTPException(status.HTTP_403_FORBIDDEN, "admin only")
    return {
        "users": [
            {"email": u.email, "role": u.role.value, "active": u.active, "activated": bool(u.password_hash)}
            for u in sorted(_auth(request).users.values(), key=lambda u: u.email)
        ]
    }


@router.post("/admin/users/invite")
def invite(body: InviteBody, request: Request, _: User = Depends(admin)) -> dict[str, Any]:
    try:
        token = _auth(request).invite(body.email, body.role)
    except AuthError as exc:
        raise HTTPException(status.HTTP_409_CONFLICT, str(exc)) from exc
    # Returned once to the admin to hand over out of band; never logged.
    return {"activation_token": token}


@router.post("/admin/users/revoke")
def revoke(body: RevokeBody, request: Request, actor: User = Depends(admin)) -> dict[str, Any]:
    if body.email.lower() == actor.email:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "cannot revoke yourself")
    try:
        ended = _auth(request).revoke(body.email)
    except AuthError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, str(exc)) from exc
    return {"ok": True, "sessions_ended": ended}


@router.post("/admin/sync")
def record_sync(body: SyncBody, request: Request, _: User = Depends(admin)) -> dict[str, Any]:
    """Stand-in for a sync job finishing. Bumps the source's cache generation."""
    engine = _engine(request)
    if body.synced_through > date.today() + timedelta(days=1):
        raise HTTPException(status.HTTP_400_BAD_REQUEST, "synced_through is in the future")
    try:
        generation = engine.warehouse.record_sync(body.source, body.synced_through)
    except KeyError as exc:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "unknown source") from exc
    return {"source": body.source, "generation": generation}
