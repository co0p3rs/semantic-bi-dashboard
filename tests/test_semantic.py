from datetime import date

import pytest

from app.semantic import (
    ContractError,
    Grain,
    Query,
    compare,
    compile_fact,
    parse_query,
    previous_period,
    run,
)

AUG = {"start_date": "2026-08-01", "end_date": "2026-08-31"}


def q(**kwargs):
    return parse_query(**{**AUG, **kwargs})


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"metrics": []}, "at least one metric"),
        ({"metrics": ["profit"]}, "unknown metric"),
        ({"metrics": ["orders"], "start_date": "2026-09-01", "end_date": "2026-08-01"}, "before"),
        ({"metrics": ["orders"], "start_date": "2025-01-01", "end_date": "2026-06-01"}, "exceeds"),
        ({"metrics": ["orders"], "grain": "hour"}, "hour"),
        ({"metrics": ["orders"], "group_by": ["customer_email"]}, "customer_email"),
        ({"metrics": ["orders"], "group_by": ["product", "product"]}, "duplicates"),
        ({"metrics": ["orders"], "filters": {"product": ["x' OR 1=1 --"]}}, "unknown product"),
        ({"metrics": ["orders"], "filters": {"channel": []}}, "empty"),
    ],
)
def test_contract_rejects(kwargs, message) -> None:
    with pytest.raises(ContractError, match=message):
        q(**kwargs)


def test_unsupported_dimension_is_an_error_not_a_zero() -> None:
    with pytest.raises(ContractError, match="conversion_rate cannot be split or filtered by product"):
        q(metrics=["conversion_rate"], group_by=["product"])
    with pytest.raises(ContractError, match="sessions"):
        q(metrics=["sessions"], filters={"product": ["trail-flask"]})


def test_compiled_sql_binds_every_value() -> None:
    query = q(metrics=["gross_revenue"], filters={"channel": ["email", "search"]}, group_by=["product"])
    sql, params = compile_fact(query, "orders")
    assert "email" not in sql and "2026-08-01" not in sql
    assert params == ["2026-08-01", "2026-08-31", "email", "search"]


def test_daily_rows_sum_to_total(shared_warehouse) -> None:
    daily = run(shared_warehouse, q(metrics=["gross_revenue", "orders"]))
    total = run(shared_warehouse, q(metrics=["gross_revenue", "orders"], grain="total"))
    assert round(sum(r["gross_revenue"] for r in daily["rows"]), 2) == total["totals"]["gross_revenue"]
    assert len(daily["rows"]) == 31


def test_mixing_facts_does_not_fan_out(shared_warehouse) -> None:
    alone = run(shared_warehouse, q(metrics=["gross_revenue"], grain="total"))
    mixed = run(shared_warehouse, q(metrics=["gross_revenue", "ad_spend", "sessions"], grain="total"))
    assert mixed["totals"]["gross_revenue"] == alone["totals"]["gross_revenue"]


def test_ratio_is_ratio_of_sums_not_mean_of_ratios(shared_warehouse) -> None:
    daily = run(shared_warehouse, q(metrics=["mer", "gross_revenue", "refunds", "ad_spend"]))
    total = run(shared_warehouse, q(metrics=["mer", "gross_revenue", "refunds", "ad_spend"], grain="total"))
    t = total["totals"]
    expected = (t["gross_revenue"] - t["refunds"]) / t["ad_spend"]
    assert t["mer"] == pytest.approx(expected, abs=1e-4)
    mean_of_daily = sum(r["mer"] for r in daily["rows"]) / len(daily["rows"])
    assert t["mer"] != pytest.approx(mean_of_daily, abs=1e-4)
    # And the totals row of a daily query obeys the same rule.
    assert daily["totals"]["mer"] == t["mer"]


def test_weeks_start_on_monday(shared_warehouse) -> None:
    result = run(shared_warehouse, q(metrics=["orders"], grain="week"))
    for row in result["rows"]:
        assert date.fromisoformat(row["bucket"]).weekday() == 0


def test_filters_narrow_and_partition(shared_warehouse) -> None:
    by_channel = run(shared_warehouse, q(metrics=["orders"], grain="total", group_by=["channel"]))
    email = run(shared_warehouse, q(metrics=["orders"], grain="total", filters={"channel": ["email"]}))
    row = next(r for r in by_channel["rows"] if r["channel"] == "email")
    assert row["orders"] == email["totals"]["orders"]
    assert sum(r["orders"] for r in by_channel["rows"]) == by_channel["totals"]["orders"]


def test_zero_denominator_is_null(shared_warehouse) -> None:
    # Ad spend is synced one day less than orders; MER over no spend is unknown, not infinite.
    spendless = run(
        shared_warehouse,
        parse_query(metrics=["mer"], start_date="2026-09-27", end_date="2026-09-27", grain="total"),
    )
    assert spendless["totals"]["mer"] is None


def test_previous_period_and_year() -> None:
    query = Query(("orders",), date(2026, 3, 1), date(2026, 3, 7), Grain.TOTAL)
    prior = previous_period(query, "previous_period")
    assert (prior.start, prior.end) == (date(2026, 2, 22), date(2026, 2, 28))
    leap = Query(("orders",), date(2028, 2, 29), date(2028, 2, 29), Grain.TOTAL)
    assert previous_period(leap, "previous_year").start == date(2027, 2, 28)
    with pytest.raises(ContractError):
        previous_period(query, "last_tuesday")


def test_compare_handles_missing_and_zero_baselines() -> None:
    out = compare({"a": 10.0, "b": 5.0, "c": None}, {"a": 8.0, "b": 0.0, "c": 1.0})
    assert out["a"]["pct_change"] == 0.25
    assert out["b"]["delta"] == 5.0 and out["b"]["pct_change"] is None
    assert out["c"]["delta"] is None


def test_canonical_form_ignores_metric_order() -> None:
    a = q(metrics=["orders", "gross_revenue"]).canonical()
    b = q(metrics=["gross_revenue", "orders"]).canonical()
    assert a == b


def test_unsynced_days_are_null_not_zero(shared_warehouse) -> None:
    # Orders are synced through 09-27, ad spend only through 09-26.
    result = run(
        shared_warehouse,
        parse_query(metrics=["gross_revenue", "ad_spend", "mer"], start_date="2026-09-26", end_date="2026-09-27"),
    )
    last_synced, unsynced = result["rows"]
    assert last_synced["ad_spend"] > 0 and last_synced["mer"] is not None
    assert unsynced["bucket"] == "2026-09-27"
    assert unsynced["gross_revenue"] > 0
    assert unsynced["ad_spend"] is None and unsynced["mer"] is None
