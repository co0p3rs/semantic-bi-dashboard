"""Read-only partner API: `/v1/*`.

An agency or finance tool wants daily revenue and spend in its own system.
It gets a narrow, versioned surface, not dashboard sessions:

* API keys are stored hashed and carry an explicit resource scope. A key
  scoped to `marketing` can read spend and MER, never session data.
* Daily grain, fixed field names, 92-day window. The schema is a contract
  with someone else's code, so it changes by adding `/v2`, not by editing.
* Per-key sliding-window rate limit, and the same semantic layer and cache
  as the dashboard, so the numbers match the dashboard to the cent.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException, Query, Request, status

from app.authz import Forbidden, Resource, require_metrics
from app.semantic import ContractError, parse_query

MAX_EXTERNAL_DAYS = 92
router = APIRouter(prefix="/v1", tags=["external"])


@dataclass(frozen=True)
class ApiKey:
    name: str
    digest: str
    resources: frozenset[Resource]


class ApiKeyStore:
    def __init__(self, *, rate_per_minute: int) -> None:
        self._keys: dict[str, ApiKey] = {}
        self._hits: dict[str, deque[float]] = defaultdict(deque)
        self._rate = rate_per_minute

    def add(self, name: str, raw_key: str, resources: frozenset[Resource]) -> ApiKey:
        key = ApiKey(name, hashlib.sha256(raw_key.encode()).hexdigest(), resources)
        self._keys[key.digest] = key
        return key

    def authenticate(self, raw_key: str | None) -> ApiKey:
        if not raw_key:
            raise HTTPException(status.HTTP_401_UNAUTHORIZED, "missing api key")
        digest = hashlib.sha256(raw_key.encode()).hexdigest()
        for stored, key in self._keys.items():
            if hmac.compare_digest(stored, digest):
                return key
        raise HTTPException(status.HTTP_401_UNAUTHORIZED, "invalid api key")

    def throttle(self, key: ApiKey, *, now: float | None = None) -> None:
        moment = time.time() if now is None else now
        window = self._hits[key.digest]
        while window and moment - window[0] >= 60:
            window.popleft()
        if len(window) >= self._rate:
            raise HTTPException(status.HTTP_429_TOO_MANY_REQUESTS, "rate limited")
        window.append(moment)


def _client(request: Request, x_api_key: str | None = Header(default=None)) -> ApiKey:
    store: ApiKeyStore = request.app.state.api_keys
    key = store.authenticate(x_api_key)
    store.throttle(key)
    return key


def _daily(request: Request, key: ApiKey, metrics: list[str], start: str, end: str) -> dict[str, Any]:
    try:
        query = parse_query(metrics=metrics, start_date=start, end_date=end, grain="day")
    except ContractError as exc:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, str(exc)) from exc
    if (query.end - query.start).days + 1 > MAX_EXTERNAL_DAYS:
        raise HTTPException(status.HTTP_400_BAD_REQUEST, f"at most {MAX_EXTERNAL_DAYS} days")
    try:
        require_metrics(key.resources, query.metrics)
    except Forbidden as exc:
        raise HTTPException(status.HTTP_403_FORBIDDEN, str(exc)) from exc
    result, meta = request.app.state.engine.execute(query)
    return {
        "schema": "northstar.v1.daily",
        "start_date": query.start.isoformat(),
        "end_date": query.end.isoformat(),
        "complete": meta["freshness"]["complete"],
        "complete_through": meta["freshness"]["complete_through"],
        # Build new dicts: `result` may be the cached object shared with the dashboard.
        "days": [
            {"date": row["bucket"], **{k: v for k, v in row.items() if k != "bucket"}}
            for row in result["rows"]
        ],
        "totals": result["totals"],
    }


@router.get("/revenue")
def revenue(
    request: Request,
    start_date: str = Query(...),
    end_date: str = Query(...),
    key: ApiKey = Depends(_client),
) -> dict[str, Any]:
    return _daily(request, key, ["gross_revenue", "refunds", "net_revenue", "orders"], start_date, end_date)


@router.get("/ad-spend")
def ad_spend(
    request: Request,
    start_date: str = Query(...),
    end_date: str = Query(...),
    key: ApiKey = Depends(_client),
) -> dict[str, Any]:
    return _daily(request, key, ["ad_spend", "mer", "cac"], start_date, end_date)
