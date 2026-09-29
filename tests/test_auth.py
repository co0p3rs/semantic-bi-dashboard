import pytest

from app.auth import AuthError, AuthService, hash_password, verify_password
from app.authz import Resource, Role, can_read_metric
from tests.conftest import PASSWORD, login

SPAN = {"start_date": "2026-09-01", "end_date": "2026-09-27"}


def service(**overrides) -> AuthService:
    params = {"idle_seconds": 100, "absolute_seconds": 1000, "max_failures": 3, "lockout_seconds": 60}
    return AuthService(**{**params, **overrides})


def test_password_hash_roundtrip_and_salt() -> None:
    a, b = hash_password("correct horse"), hash_password("correct horse")
    assert a != b
    assert verify_password("correct horse", a) and not verify_password("wrong", a)
    assert not verify_password("x", "garbage")


def test_session_token_is_not_stored_in_clear() -> None:
    auth = service()
    auth.create_user("a@x.example", Role.VIEWER, "long-enough-pass")
    token, _ = auth.login("a@x.example", "long-enough-pass")
    assert token not in auth._sessions


def test_idle_and_absolute_expiry() -> None:
    auth = service()
    auth.create_user("a@x.example", Role.VIEWER, "long-enough-pass")
    token, _ = auth.login("a@x.example", "long-enough-pass", now=0)
    auth.resolve(token, now=90)
    auth.resolve(token, now=180)  # activity keeps it alive
    with pytest.raises(AuthError):
        auth.resolve(token, now=300)  # idle > 100

    token, _ = auth.login("a@x.example", "long-enough-pass", now=0)
    for t in range(50, 1000, 50):
        auth.resolve(token, now=t)
    with pytest.raises(AuthError):
        auth.resolve(token, now=1001)  # absolute limit despite activity


def test_lockout_applies_even_with_the_right_password() -> None:
    auth = service()
    auth.create_user("a@x.example", Role.VIEWER, "long-enough-pass")
    for _ in range(3):
        with pytest.raises(AuthError):
            auth.login("a@x.example", "nope", now=0)
    with pytest.raises(AuthError):
        auth.login("a@x.example", "long-enough-pass", now=30)
    auth.login("a@x.example", "long-enough-pass", now=61)


def test_unknown_email_and_wrong_password_look_identical() -> None:
    auth = service()
    auth.create_user("a@x.example", Role.VIEWER, "long-enough-pass")
    with pytest.raises(AuthError) as unknown:
        auth.login("ghost@x.example", "whatever")
    with pytest.raises(AuthError) as wrong:
        auth.login("a@x.example", "whatever")
    assert str(unknown.value) == str(wrong.value)


def test_invite_is_single_use_and_expires() -> None:
    auth = service()
    token = auth.invite("new@x.example", Role.MARKETER, now=0)
    with pytest.raises(AuthError, match="at least"):
        auth.activate(token, "short", now=1)
    auth.activate(token, "a-long-new-password", now=1)
    with pytest.raises(AuthError):
        auth.activate(token, "a-long-new-password", now=2)

    late = auth.invite("late@x.example", Role.VIEWER, now=0)
    with pytest.raises(AuthError):
        auth.activate(late, "a-long-new-password", now=10**7)


def test_derived_metric_needs_every_input() -> None:
    overview_only = frozenset({Resource.OVERVIEW})
    assert can_read_metric(overview_only, "net_revenue")
    assert not can_read_metric(overview_only, "mer")  # would reveal spend
    assert can_read_metric(frozenset({Resource.OVERVIEW, Resource.MARKETING}), "mer")


# --- HTTP ---------------------------------------------------------------


def test_data_routes_need_a_session(client) -> None:
    assert client.post("/api/v1/dashboard/overview", json=SPAN).status_code == 401
    assert client.get("/api/v1/catalog").status_code == 401


def test_session_cookie_is_httponly(client) -> None:
    response = client.post(
        "/api/auth/login", json={"email": "viewer@northstar.example", "password": PASSWORD}
    )
    cookies = response.headers.get_list("set-cookie")
    session = next(c for c in cookies if c.startswith("nbi_session="))
    csrf = next(c for c in cookies if c.startswith("nbi_csrf="))
    assert "HttpOnly" in session and "SameSite=lax" in session
    assert "HttpOnly" not in csrf


def test_viewer_sees_a_narrower_dashboard(client) -> None:
    login(client, "viewer@northstar.example")
    data = client.post("/api/v1/dashboard/overview", json=SPAN).json()
    assert "net_revenue" in data["tiles"]
    assert "mer" not in data["tiles"] and "cac" not in data["tiles"]
    assert data["series_metrics"] == ["net_revenue"]
    names = {m["metric"] for m in client.get("/api/v1/catalog").json()["metrics"]}
    assert "ad_spend" not in names and "gross_revenue" in names

    denied = client.post("/api/v1/query", json={**SPAN, "metrics": ["mer"]})
    assert denied.status_code == 403


def test_mutations_require_csrf(owner) -> None:
    client, csrf = owner
    body = {"email": "new@northstar.example", "role": "viewer"}
    assert client.post("/api/admin/users/invite", json=body).status_code == 403
    assert client.post("/api/admin/users/invite", json=body, headers={"x-csrf-token": "forged"}).status_code == 403
    ok = client.post("/api/admin/users/invite", json=body, headers={"x-csrf-token": csrf})
    assert ok.status_code == 200 and ok.json()["activation_token"]


def test_non_admin_cannot_manage_users(client) -> None:
    csrf = login(client, "marketer@northstar.example")
    response = client.post(
        "/api/admin/users/invite",
        json={"email": "x@northstar.example", "role": "admin"},
        headers={"x-csrf-token": csrf},
    )
    assert response.status_code == 403
    assert client.get("/api/admin/users").status_code == 403


def test_invite_activate_login_flow(owner) -> None:
    client, csrf = owner
    token = client.post(
        "/api/admin/users/invite",
        json={"email": "analyst@northstar.example", "role": "marketer"},
        headers={"x-csrf-token": csrf},
    ).json()["activation_token"]
    assert client.post("/api/auth/activate", json={"token": token, "password": "analyst-password-1"}).status_code == 200
    login_response = client.post(
        "/api/auth/login", json={"email": "analyst@northstar.example", "password": "analyst-password-1"}
    )
    assert login_response.status_code == 200
    assert client.get("/api/me").json()["role"] == "marketer"


def test_revoke_ends_live_sessions(app) -> None:
    from fastapi.testclient import TestClient

    admin_client, victim = TestClient(app), TestClient(app)
    csrf = login(admin_client, "owner@northstar.example")
    login(victim, "marketer@northstar.example")
    assert victim.get("/api/me").status_code == 200

    response = admin_client.post(
        "/api/admin/users/revoke",
        json={"email": "marketer@northstar.example"},
        headers={"x-csrf-token": csrf},
    )
    assert response.json()["sessions_ended"] == 1
    assert victim.get("/api/me").status_code == 401

    self_revoke = admin_client.post(
        "/api/admin/users/revoke", json={"email": "owner@northstar.example"}, headers={"x-csrf-token": csrf}
    )
    assert self_revoke.status_code == 400


def test_logout_invalidates_server_side(owner) -> None:
    client, csrf = owner
    token = client.cookies.get("nbi_session")
    assert client.post("/api/auth/logout", headers={"x-csrf-token": csrf}).status_code == 200
    client.cookies.set("nbi_session", token)  # replay the old cookie
    assert client.get("/api/me").status_code == 401
