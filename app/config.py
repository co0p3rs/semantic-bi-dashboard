"""Process settings. Secrets come from the environment, never from source."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    environment: str = "development"
    warehouse_path: str = ":memory:"
    seed: int = 7

    # Local demo accounts are created with this password. Production has no
    # default: accounts are invited and set their own password.
    demo_password: SecretStr = SecretStr("northstar-demo")

    session_idle_seconds: int = 30 * 60
    session_absolute_seconds: int = 12 * 3600
    login_max_failures: int = 5
    login_lockout_seconds: int = 15 * 60
    cookie_secure: bool = False

    cache_ttl_seconds: int = 24 * 3600
    cache_max_entries: int = 512

    external_rate_limit_per_minute: int = 30

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    @property
    def docs_enabled(self) -> bool:
        return self.environment != "production"


@lru_cache
def get_settings() -> Settings:
    return Settings()
