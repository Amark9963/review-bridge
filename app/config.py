from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    database_url: str
    # "live" is added only once eligible Yelp access is verified.
    yelp_mode: Literal["synthetic"] = "synthetic"
    # Minimum seconds between syncs of one connection, protecting provider quota.
    sync_cooldown_seconds: int = 300
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")


@lru_cache
def settings() -> Settings:
    return Settings()
