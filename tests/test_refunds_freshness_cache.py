from datetime import date

import pytest

from app.cache import GenerationCache
from app.semantic import parse_query, run

JUNE = {"start_date": "2026-06-01", "end_date": "2026-06-30", "grain": "total"}


def test_net_revenue_uses_the_complete_ledger_not_the_order_snapshot(warehouse) -> None:
    ledger = run(warehouse, parse_query(metrics=["refunds", "net_revenue", "gross_revenue"], **JUNE))["totals"]
    [snapshot] = warehouse.query(
        "SELECT SUM(refunded_snapshot) AS s FROM orders WHERE order_date BETWEEN ? AND ?",
        ["2026-06-01", "2026-06-30"],
    )
    # The snapshot misses every refund issued more than a few days after the order.
    assert snapshot["s"] < ledger["refunds"]
    assert ledger["net_revenue"] == pytest.approx(ledger["gross_revenue"] - ledger["refunds"], abs=0.01)


def test_late_refund_restates_the_original_order_date(warehouse) -> None:
    before = run(warehouse, parse_query(metrics=["net_revenue"], **JUNE))["totals"]["net_revenue"]
    [order] = warehouse.query("SELECT id FROM orders WHERE order_date = '2026-06-15' LIMIT 1", [])
    warehouse.add_refund(order["id"], date(2026, 9, 20), 50.0)
    after = run(warehouse, parse_query(metrics=["net_revenue"], **JUNE))["totals"]["net_revenue"]
    assert after == pytest.approx(before - 50.0, abs=0.01)


def test_freshness_names_the_lagging_source(engine) -> None:
    query = parse_query(metrics=["mer"], start_date="2026-09-20", end_date="2026-09-27", grain="total")
    _, meta = engine.execute(query)
    fresh = meta["freshness"]
    assert fresh["complete"] is False
    assert fresh["complete_through"] == "2026-09-26"
    assert any("ad_spend" in w for w in fresh["warnings"])

    revenue_only = parse_query(metrics=["gross_revenue"], start_date="2026-09-20", end_date="2026-09-27")
    assert engine.execute(revenue_only)[1]["freshness"]["complete"] is True


def test_repeat_query_is_a_cache_hit(engine) -> None:
    query = parse_query(metrics=["orders"], **JUNE)
    assert engine.execute(query)[1]["cache"]["hit"] is False
    assert engine.execute(query)[1]["cache"]["hit"] is True


def test_sync_invalidates_only_queries_that_read_that_source(engine) -> None:
    revenue = parse_query(metrics=["gross_revenue"], **JUNE)
    mer = parse_query(metrics=["mer"], **JUNE)
    engine.execute(revenue)
    engine.execute(mer)

    engine.warehouse.record_sync("ad_spend", date(2026, 9, 27))

    assert engine.execute(revenue)[1]["cache"]["hit"] is True
    result, meta = engine.execute(mer)
    assert meta["cache"]["hit"] is False
    assert meta["cache"]["generations"]["ad_spend"] == 2


def test_sync_updates_freshness_through_the_new_generation(engine) -> None:
    query = parse_query(metrics=["ad_spend"], start_date="2026-09-20", end_date="2026-09-27")
    assert engine.execute(query)[1]["freshness"]["complete"] is False
    engine.warehouse.record_sync("ad_spend", date(2026, 9, 27))
    assert engine.execute(query)[1]["freshness"]["complete"] is True


def test_cache_ttl_and_lru_bound() -> None:
    cache = GenerationCache(ttl_seconds=10, max_entries=2)
    cache.put("a", 1, now=0)
    assert cache.get("a", now=9) == 1
    assert cache.get("a", now=10) is None
    cache.put("a", 1, now=0)
    cache.put("b", 2, now=0)
    cache.get("a", now=1)  # a is now most recently used
    cache.put("c", 3, now=1)
    assert cache.get("b", now=1) is None
    assert cache.get("a", now=1) == 1 and len(cache) == 2


def test_cache_key_depends_on_generations_not_dict_order() -> None:
    k1 = GenerationCache.key("q", {"a": 1, "b": 2}, {"orders": 1, "refunds": 1})
    k2 = GenerationCache.key("q", {"b": 2, "a": 1}, {"refunds": 1, "orders": 1})
    k3 = GenerationCache.key("q", {"a": 1, "b": 2}, {"orders": 2, "refunds": 1})
    assert k1 == k2 != k3
