"""Resource-based authorization for metrics.

Permissions are granted on dashboard *resources*, not on metric names or
prefixes. A metric maps to one or more resources; the caller needs any one
of them. A derived metric is only as visible as its least visible input:
MER needs spend, so a user who cannot see spend cannot back it out of MER.
"""

from __future__ import annotations

from enum import StrEnum

from app.semantic import BASE_METRICS, base_inputs


class Resource(StrEnum):
    OVERVIEW = "overview"
    MARKETING = "marketing"
    TRAFFIC = "traffic"


class Capability(StrEnum):
    USERS_MANAGE = "users.manage"


class Role(StrEnum):
    VIEWER = "viewer"
    MARKETER = "marketer"
    ADMIN = "admin"


ROLE_RESOURCES: dict[Role, frozenset[Resource]] = {
    Role.VIEWER: frozenset({Resource.OVERVIEW}),
    Role.MARKETER: frozenset({Resource.OVERVIEW, Resource.MARKETING, Resource.TRAFFIC}),
    Role.ADMIN: frozenset(Resource),
}

ROLE_CAPABILITIES: dict[Role, frozenset[Capability]] = {
    Role.VIEWER: frozenset(),
    Role.MARKETER: frozenset(),
    Role.ADMIN: frozenset({Capability.USERS_MANAGE}),
}

# Base metric -> resources that expose it. Spend is shown on the Overview
# (blended MER tile) and on Marketing, so either grant is enough.
BASE_METRIC_RESOURCES: dict[str, frozenset[Resource]] = {
    "gross_revenue": frozenset({Resource.OVERVIEW}),
    "orders": frozenset({Resource.OVERVIEW}),
    "new_customers": frozenset({Resource.OVERVIEW, Resource.MARKETING}),
    "refunds": frozenset({Resource.OVERVIEW}),
    "ad_spend": frozenset({Resource.MARKETING}),
    "sessions": frozenset({Resource.TRAFFIC}),
}
assert set(BASE_METRIC_RESOURCES) == set(BASE_METRICS), "every metric needs a resource"


class Forbidden(PermissionError):
    pass


def can_read_metric(resources: frozenset[Resource], metric: str) -> bool:
    return all(BASE_METRIC_RESOURCES[name] & resources for name in base_inputs(metric))


def require_metrics(resources: frozenset[Resource], metrics: tuple[str, ...] | list[str]) -> None:
    denied = sorted(m for m in metrics if not can_read_metric(resources, m))
    if denied:
        raise Forbidden(f"not permitted: {', '.join(denied)}")
