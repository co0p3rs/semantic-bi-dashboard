import pytest
from fastapi.testclient import TestClient

from app.cache import GenerationCache
from app.config import Settings
from app.engine import Engine
from app.main import create_app
from app.warehouse import Warehouse

PASSWORD = "northstar-demo"


@pytest.fixture(scope="session")
def shared_warehouse() -> Warehouse:
    """Read-only tests share one seeded warehouse; seeding is the slow part."""
    return Warehouse()


@pytest.fixture
def warehouse() -> Warehouse:
    """Fresh warehouse for tests that write (syncs, late refunds)."""
    return Warehouse()


@pytest.fixture
def engine(warehouse: Warehouse) -> Engine:
    return Engine(warehouse, GenerationCache(ttl_seconds=3600, max_entries=64))


@pytest.fixture
def app():
    return create_app(Settings(external_rate_limit_per_minute=5))


@pytest.fixture
def client(app) -> TestClient:
    return TestClient(app)


def login(client: TestClient, email: str) -> str:
    response = client.post("/api/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return client.cookies.get("nbi_csrf")


@pytest.fixture
def owner(client: TestClient) -> tuple[TestClient, str]:
    return client, login(client, "owner@northstar.example")
