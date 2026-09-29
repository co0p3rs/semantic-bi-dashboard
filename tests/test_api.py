from tests.conftest import login

WEEK = {"start_date": "2026-09-14", "end_date": "2026-09-20"}


def test_index_and_health_and_headers(client) -> None:
    index = client.get("/")
    assert index.status_code == 200 and "Northstar BI" in index.text
    assert "script-src 'self'" in index.headers["content-security-policy"]
    assert index.headers["x-frame-options"] == "DENY"
    health = client.get("/healthz").json()
    assert health["status"] == "ok" and "orders" in health["sources"]


def test_api_responses_are_not_cacheable_by_browsers(owner) -> None:
    client, _ = owner
    assert client.get("/api/me").headers["cache-control"] == "no-store"


def test_overview_shape(owner) -> None:
    client, _ = owner
    data = client.post("/api/v1/dashboard/overview", json=WEEK).json()
    assert set(data["tiles"]) >= {"net_revenue", "mer", "cac", "conversion_rate"}
    assert len(data["series"]) == 7
    assert data["previous_range"] == ["2026-09-07", "2026-09-13"]
    revenues = [row["net_revenue"] for row in data["by_product"]]
    assert revenues == sorted(revenues, reverse=True)


def test_query_contract_errors_are_400(owner) -> None:
    client, _ = owner
    response = client.post("/api/v1/query", json={**WEEK, "metrics": ["DROP TABLE orders"]})
    assert response.status_code == 400 and "unknown metric" in response.json()["detail"]


def test_comparison_endpoint(owner) -> None:
    client, _ = owner
    data = client.post("/api/v1/comparison", json={**WEEK, "metrics": ["orders"], "compare": "previous_year"}).json()
    assert data["previous_range"] == ["2025-09-14", "2025-09-20"]
    # No 2025 data in the synthetic warehouse: the change is unknown, not +inf.
    assert data["metrics"]["orders"]["pct_change"] is None


def test_admin_sync_bumps_generation_and_refreshes_freshness(owner) -> None:
    client, csrf = owner
    body = {**WEEK, "end_date": "2026-09-27", "metrics": ["ad_spend"]}
    first = client.post("/api/v1/query", json=body).json()["meta"]
    assert first["freshness"]["complete"] is False
    sync = client.post(
        "/api/admin/sync",
        json={"source": "ad_spend", "synced_through": "2026-09-27"},
        headers={"x-csrf-token": csrf},
    )
    assert sync.json()["generation"] == 2
    second = client.post("/api/v1/query", json=body).json()["meta"]
    assert second["cache"]["hit"] is False and second["freshness"]["complete"] is True


# --- external API -------------------------------------------------------


def test_external_requires_a_valid_key(client) -> None:
    assert client.get("/v1/revenue", params=WEEK).status_code == 401
    assert client.get("/v1/revenue", params=WEEK, headers={"x-api-key": "guess"}).status_code == 401


def test_external_key_scope(client) -> None:
    finance = {"x-api-key": "demo-finance-key"}
    assert client.get("/v1/revenue", params=WEEK, headers=finance).status_code == 200
    assert client.get("/v1/ad-spend", params=WEEK, headers=finance).status_code == 403
    agency = {"x-api-key": "demo-agency-key"}
    assert client.get("/v1/ad-spend", params=WEEK, headers=agency).status_code == 200


def test_external_numbers_match_the_dashboard(client) -> None:
    external = client.get("/v1/revenue", params=WEEK, headers={"x-api-key": "demo-finance-key"}).json()
    login(client, "owner@northstar.example")
    internal = client.post("/api/v1/query", json={**WEEK, "metrics": ["net_revenue"], "grain": "total"}).json()
    assert external["totals"]["net_revenue"] == internal["totals"]["net_revenue"]
    assert external["schema"] == "northstar.v1.daily"
    assert [d["date"] for d in external["days"]][0] == "2026-09-14"


def test_external_window_and_rate_limit(client) -> None:
    key = {"x-api-key": "demo-agency-key"}
    too_long = client.get("/v1/revenue", params={"start_date": "2026-01-01", "end_date": "2026-06-30"}, headers=key)
    assert too_long.status_code == 400
    statuses = [client.get("/v1/revenue", params=WEEK, headers=key).status_code for _ in range(6)]
    assert statuses[-1] == 429
