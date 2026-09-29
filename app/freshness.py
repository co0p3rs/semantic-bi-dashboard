"""Completeness metadata attached to every number.

A result is *complete* only if every sync it depends on has reached the
end of the requested range. Incomplete results are still returned (a
partial "today" is useful), but the response says which source is behind
and by how much, and the UI marks the tile instead of letting it look final.
"""

from __future__ import annotations

from datetime import date

from app.semantic import Query
from app.warehouse import Warehouse


def freshness_for(warehouse: Warehouse, query: Query) -> dict[str, object]:
    state = warehouse.sync_state()
    sources = []
    for source in sorted(query.sources):
        through = date.fromisoformat(str(state[source]["synced_through"]))
        missing = max(0, (query.end - through).days)
        sources.append(
            {"source": source, "synced_through": through.isoformat(), "missing_days": missing}
        )
    lagging = [s for s in sources if s["missing_days"]]
    return {
        "complete": not lagging,
        # The latest day every source in this query has fully covered.
        "complete_through": min(s["synced_through"] for s in sources) if sources else None,
        "sources": sources,
        "warnings": [
            f"{s['source']} is synced through {s['synced_through']}; "
            f"the last {s['missing_days']} day(s) are partial"
            for s in lagging
        ],
    }


def generations(warehouse: Warehouse, sources: frozenset[str]) -> dict[str, int]:
    state = warehouse.sync_state()
    return {source: int(state[source]["generation"]) for source in sorted(sources)}
