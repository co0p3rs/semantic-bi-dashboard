"""The semantic layer: a closed vocabulary compiled to parameterised SQL.

The dashboard, the external API, and anything else that reads numbers go
through here. Callers name metrics and dimensions; they never send SQL,
column names, or join hints. Three rules carry most of the weight:

1. **One query per fact table, merged in Python.** Joining orders to ad
   spend to sessions on day fans rows out and multiplies revenue by the
   number of campaigns. Each fact is aggregated at the requested grain on
   its own, then buckets are merged on (bucket, dimensions).
2. **Ratios are computed from sums, after aggregation.** Weekly MER is
   weekly net revenue over weekly spend, not the mean of seven daily MERs.
   The same rule produces the totals row, so the total is never a sum of
   ratios.
3. **A dimension a fact cannot answer is an error, not a zero.** Sessions
   are not tracked per product. "Conversion rate by product" is refused
   with the reason, instead of silently dividing product orders by all
   sessions.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, timedelta
from enum import StrEnum

from app.warehouse import CHANNELS, PRODUCTS, Warehouse

MAX_DAYS = 366
MAX_ROWS = 500
MAX_METRICS = 8


class ContractError(ValueError):
    """The request is outside the vocabulary. Safe to show to the caller."""


class Grain(StrEnum):
    DAY = "day"
    WEEK = "week"
    MONTH = "month"
    TOTAL = "total"


class Dimension(StrEnum):
    CHANNEL = "channel"
    PRODUCT = "product"


DIMENSION_VALUES: dict[Dimension, frozenset[str]] = {
    Dimension.CHANNEL: frozenset(CHANNELS),
    Dimension.PRODUCT: frozenset(PRODUCTS),
}


@dataclass(frozen=True)
class Fact:
    name: str
    from_sql: str
    date_col: str
    dimensions: dict[Dimension, str]
    # Syncs whose completeness this fact depends on.
    sources: tuple[str, ...]


FACTS: dict[str, Fact] = {
    "orders": Fact(
        "orders",
        "orders o",
        "o.order_date",
        {Dimension.CHANNEL: "o.channel", Dimension.PRODUCT: "o.product"},
        ("orders",),
    ),
    # Refunds are attributed to the *order* date so net revenue for a past
    # week keeps improving in accuracy as late refunds arrive, rather than
    # dumping them into whatever day the refund happened.
    "refunds": Fact(
        "refunds",
        "refunds r JOIN orders o ON o.id = r.order_id",
        "o.order_date",
        {Dimension.CHANNEL: "o.channel", Dimension.PRODUCT: "o.product"},
        ("orders", "refunds"),
    ),
    "ad_spend": Fact(
        "ad_spend",
        "ad_spend a",
        "a.day",
        {Dimension.CHANNEL: "a.channel", Dimension.PRODUCT: "a.product"},
        ("ad_spend",),
    ),
    "sessions": Fact(
        "sessions",
        "sessions s",
        "s.day",
        {Dimension.CHANNEL: "s.channel"},
        ("sessions",),
    ),
}


class Unit(StrEnum):
    CURRENCY = "currency"
    COUNT = "count"
    RATIO = "ratio"
    PERCENT = "percent"


@dataclass(frozen=True)
class BaseMetric:
    name: str
    fact: str
    expr: str
    unit: Unit
    description: str


@dataclass(frozen=True)
class DerivedMetric:
    name: str
    inputs: tuple[str, ...]
    compute: Callable[..., float | None]
    unit: Unit
    description: str


def _div(a: float, b: float) -> float | None:
    return None if not b else a / b


BASE_METRICS: dict[str, BaseMetric] = {
    m.name: m
    for m in (
        BaseMetric("gross_revenue", "orders", "SUM(o.gross)", Unit.CURRENCY, "Order value before refunds."),
        BaseMetric("orders", "orders", "COUNT(*)", Unit.COUNT, "Orders placed."),
        BaseMetric("new_customers", "orders", "SUM(o.is_first_order)", Unit.COUNT, "First-ever orders."),
        BaseMetric(
            "refunds",
            "refunds",
            "SUM(r.amount)",
            Unit.CURRENCY,
            "Refunds from the complete ledger, attributed to order date.",
        ),
        BaseMetric("ad_spend", "ad_spend", "SUM(a.spend)", Unit.CURRENCY, "Paid media spend."),
        BaseMetric("sessions", "sessions", "SUM(s.sessions)", Unit.COUNT, "Site sessions."),
    )
}

DERIVED_METRICS: dict[str, DerivedMetric] = {
    m.name: m
    for m in (
        DerivedMetric(
            "net_revenue",
            ("gross_revenue", "refunds"),
            lambda gross, refunds: gross - refunds,
            Unit.CURRENCY,
            "Gross revenue minus ledger refunds.",
        ),
        DerivedMetric(
            "average_order_value",
            ("gross_revenue", "orders"),
            _div,
            Unit.CURRENCY,
            "Gross revenue / orders.",
        ),
        DerivedMetric(
            "mer",
            ("gross_revenue", "refunds", "ad_spend"),
            lambda gross, refunds, spend: _div(gross - refunds, spend),
            Unit.RATIO,
            "Marketing efficiency: net revenue / ad spend.",
        ),
        DerivedMetric(
            "cac",
            ("ad_spend", "new_customers"),
            _div,
            Unit.CURRENCY,
            "Blended acquisition cost: ad spend / new customers.",
        ),
        DerivedMetric(
            "conversion_rate",
            ("orders", "sessions"),
            _div,
            Unit.PERCENT,
            "Orders / sessions.",
        ),
        DerivedMetric(
            "refund_rate",
            ("refunds", "gross_revenue"),
            _div,
            Unit.PERCENT,
            "Ledger refunds / gross revenue.",
        ),
    )
}

ALL_METRICS = frozenset(BASE_METRICS) | frozenset(DERIVED_METRICS)


def base_inputs(metric: str) -> tuple[str, ...]:
    if metric in BASE_METRICS:
        return (metric,)
    return DERIVED_METRICS[metric].inputs


def facts_for(metric: str) -> frozenset[str]:
    return frozenset(BASE_METRICS[name].fact for name in base_inputs(metric))


def supported_dimensions(metric: str) -> frozenset[Dimension]:
    return frozenset.intersection(*(frozenset(FACTS[f].dimensions) for f in facts_for(metric)))


@dataclass(frozen=True)
class Query:
    metrics: tuple[str, ...]
    start: date
    end: date
    grain: Grain = Grain.DAY
    group_by: tuple[Dimension, ...] = ()
    filters: dict[Dimension, tuple[str, ...]] = field(default_factory=dict)

    def canonical(self) -> dict[str, object]:
        """Stable form for cache keys: order-insensitive where order is meaningless."""
        return {
            "metrics": sorted(self.metrics),
            "start": self.start.isoformat(),
            "end": self.end.isoformat(),
            "grain": self.grain.value,
            "group_by": [d.value for d in self.group_by],
            "filters": {d.value: sorted(v) for d, v in sorted(self.filters.items())},
        }

    @property
    def facts(self) -> frozenset[str]:
        return frozenset().union(*(facts_for(m) for m in self.metrics))

    @property
    def sources(self) -> frozenset[str]:
        return frozenset(s for f in self.facts for s in FACTS[f].sources)


def parse_query(
    *,
    metrics: list[str],
    start_date: str,
    end_date: str,
    grain: str = "day",
    group_by: list[str] | None = None,
    filters: dict[str, list[str]] | None = None,
) -> Query:
    if not metrics:
        raise ContractError("at least one metric is required")
    if len(metrics) > MAX_METRICS:
        raise ContractError(f"at most {MAX_METRICS} metrics per query")
    unknown = [m for m in metrics if m not in ALL_METRICS]
    if unknown:
        raise ContractError(f"unknown metric: {', '.join(unknown)}")
    try:
        start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
    except ValueError as exc:
        raise ContractError("dates must be YYYY-MM-DD") from exc
    if end < start:
        raise ContractError("end_date is before start_date")
    if (end - start).days + 1 > MAX_DAYS:
        raise ContractError(f"date range exceeds {MAX_DAYS} days")
    try:
        parsed_grain = Grain(grain)
        dims = tuple(Dimension(d) for d in (group_by or []))
        parsed_filters = {Dimension(k): tuple(v) for k, v in (filters or {}).items()}
    except ValueError as exc:
        raise ContractError(str(exc)) from exc
    if len(set(dims)) != len(dims):
        raise ContractError("group_by has duplicates")

    for dim, values in parsed_filters.items():
        if not values:
            raise ContractError(f"filter {dim.value} is empty")
        bad = sorted(set(values) - DIMENSION_VALUES[dim])
        if bad:
            raise ContractError(f"unknown {dim.value}: {', '.join(bad)}")

    needed = set(dims) | set(parsed_filters)
    for metric in metrics:
        missing = needed - supported_dimensions(metric)
        if missing:
            names = ", ".join(sorted(d.value for d in missing))
            raise ContractError(f"{metric} cannot be split or filtered by {names}")

    return Query(
        metrics=tuple(dict.fromkeys(metrics)),
        start=start,
        end=end,
        grain=parsed_grain,
        group_by=dims,
        filters=parsed_filters,
    )


def _bucket_sql(grain: Grain, col: str) -> str:
    if grain is Grain.DAY:
        return col
    if grain is Grain.WEEK:
        # Monday-start ISO weeks.
        return f"date({col}, '-6 days', 'weekday 1')"
    if grain is Grain.MONTH:
        return f"strftime('%Y-%m-01', {col})"
    return "'total'"


def compile_fact(query: Query, fact_name: str) -> tuple[str, list[object]]:
    """Every identifier comes from the catalog; every value is a bound parameter."""
    fact = FACTS[fact_name]
    metrics = sorted(
        {name for m in query.metrics for name in base_inputs(m) if BASE_METRICS[name].fact == fact_name}
    )
    dim_cols = [fact.dimensions[d] for d in query.group_by]
    select = [f"{_bucket_sql(query.grain, fact.date_col)} AS bucket"]
    select += [f"{col} AS {d.value}" for col, d in zip(dim_cols, query.group_by, strict=True)]
    select += [f"COALESCE({BASE_METRICS[m].expr}, 0) AS {m}" for m in metrics]
    where = [f"{fact.date_col} BETWEEN ? AND ?"]
    params: list[object] = [query.start.isoformat(), query.end.isoformat()]
    for dim, values in sorted(query.filters.items()):
        where.append(f"{fact.dimensions[dim]} IN ({', '.join('?' for _ in values)})")
        params.extend(sorted(values))
    group = ["bucket", *(d.value for d in query.group_by)]
    sql = (
        f"SELECT {', '.join(select)} FROM {fact.from_sql} "
        f"WHERE {' AND '.join(where)} GROUP BY {', '.join(group)}"
    )
    return sql, params


def _finish(base: dict[str, float | None], metrics: tuple[str, ...]) -> dict[str, float | None]:
    """A missing base value is zero (nothing happened); an explicit None is unknown."""
    out: dict[str, float | None] = {}
    for metric in metrics:
        if metric in BASE_METRICS:
            value = base.get(metric, 0.0)
            out[metric] = None if value is None else round(value, 2)
            continue
        spec = DERIVED_METRICS[metric]
        inputs = [base.get(name, 0.0) for name in spec.inputs]
        if any(v is None for v in inputs):
            out[metric] = None
            continue
        value = spec.compute(*inputs)
        out[metric] = None if value is None else round(value, 4 if spec.unit is not Unit.CURRENCY else 2)
    return out


def coverage(warehouse: Warehouse, query: Query) -> dict[str, str]:
    """Last day each fact is fully synced: the earliest of the syncs it needs."""
    state = warehouse.sync_state()
    return {
        fact: min(str(state[source]["synced_through"]) for source in FACTS[fact].sources)
        for fact in query.facts
    }


def run(warehouse: Warehouse, query: Query) -> dict[str, object]:
    merged: dict[tuple[str, ...], dict[str, float | None]] = {}
    for fact_name in sorted(query.facts):
        sql, params = compile_fact(query, fact_name)
        for row in warehouse.query(sql, params):
            key = (str(row["bucket"]), *(str(row[d.value]) for d in query.group_by))
            bucket = merged.setdefault(key, {})
            for column in row.keys():
                if column in BASE_METRICS:
                    bucket[column] = bucket.get(column, 0.0) + float(row[column])

    totals: dict[str, float | None] = {}
    for values in merged.values():
        for name, value in values.items():
            totals[name] = (totals.get(name) or 0.0) + (value or 0.0)

    # A day past a fact's sync is unknown, not zero. Without this, the last
    # day of every chart shows spend "dropping to nothing" until ads catch up.
    if query.grain is Grain.DAY:
        covered = coverage(warehouse, query)
        needed = {name for m in query.metrics for name in base_inputs(m)}
        for key, values in merged.items():
            for name in needed:
                if key[0] > covered[BASE_METRICS[name].fact]:
                    values[name] = None

    keys = sorted(merged)
    rows = [
        {
            "bucket": key[0],
            **{d.value: key[i + 1] for i, d in enumerate(query.group_by)},
            **_finish(merged[key], query.metrics),
        }
        for key in keys[:MAX_ROWS]
    ]
    return {
        "rows": rows,
        "totals": _finish(totals, query.metrics),
        "truncated": len(keys) > MAX_ROWS,
    }


def previous_period(query: Query, mode: str) -> Query:
    if mode == "previous_period":
        span = (query.end - query.start).days + 1
        start = query.start - timedelta(days=span)
        end = query.start - timedelta(days=1)
    elif mode == "previous_year":
        start, end = _shift_year(query.start), _shift_year(query.end)
    else:
        raise ContractError("compare must be previous_period or previous_year")
    return Query(query.metrics, start, end, query.grain, query.group_by, dict(query.filters))


def _shift_year(value: date) -> date:
    try:
        return value.replace(year=value.year - 1)
    except ValueError:  # 29 February
        return value.replace(year=value.year - 1, day=28)


def compare(current: dict[str, float | None], prior: dict[str, float | None]) -> dict[str, object]:
    out: dict[str, object] = {}
    for metric, now in current.items():
        before = prior.get(metric)
        delta = None if now is None or before is None else round(now - before, 4)
        pct = None if delta is None or not before else round(delta / abs(before), 4)
        out[metric] = {"current": now, "previous": before, "delta": delta, "pct_change": pct}
    return out


def catalog() -> list[dict[str, object]]:
    items: list[dict[str, object]] = []
    for name in sorted(ALL_METRICS):
        spec = BASE_METRICS.get(name) or DERIVED_METRICS[name]
        items.append(
            {
                "metric": name,
                "unit": spec.unit.value,
                "description": spec.description,
                "dimensions": sorted(d.value for d in supported_dimensions(name)),
                "derived": name in DERIVED_METRICS,
            }
        )
    return items
