from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    # "live" is added only once eligible Yelp access is verified.
    yelp_mode: Literal["synthetic"] = "synthetic"
    # Minimum seconds between syncs of one connection, protecting provider quota.
    sync_cooldown_seconds: int = 300
    # How often each active connection is synced automatically. Yelp content may be kept
    # for 24 hours, so 12 leaves room for one missed run.
    sync_interval_hours: float = 12
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"
    # Requests per minute, per token, for each endpoint group.
    rate_limit_feed_per_minute: int = 300
    rate_limit_tokens_per_minute: int = 30
    rate_limit_default_per_minute: int = 120
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def settings() -> Settings:
    return Settings()
