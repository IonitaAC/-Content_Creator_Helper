"""
StreamScout & GigHunt — Centralised Configuration
===================================================
Reads all environment variables via pydantic-settings.
Validates types and provides sensible defaults at import time.
"""

from __future__ import annotations

from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    """Application-wide settings loaded from ``.env``."""

    model_config = SettingsConfigDict(
        env_file=str(_ENV_FILE),
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ── Database (SQLite by default — zero install) ─────────
    database_url: str = "sqlite+aiosqlite:///./streamscout.db"
    database_url_sync: str = "sqlite:///./streamscout.db"

    # ── Redis (optional — leave blank for in-memory dedup) ──
    redis_url: str = ""

    # ── Twitch ────────────────────────────────────────────────
    twitch_client_id: str = ""
    twitch_client_secret: str = ""

    # ── YouTube ───────────────────────────────────────────────
    youtube_api_key: str = ""

    # ── Twitter / X (twikit cookie auth) ──────────────────────
    twitter_auth_token: str = ""
    twitter_ct0: str = ""
    twitter_username: str = ""
    twitter_email: str = ""
    twitter_password: str = ""

    # ── Reddit ────────────────────────────────────────────────
    reddit_client_id: str = ""
    reddit_client_secret: str = ""
    reddit_user_agent: str = "StreamScout/1.0"

    # ── Thresholds ────────────────────────────────────────────
    min_viewers: int = 1_000
    min_followers: int = 100_000
    dormancy_days: int = 180

    # ── Filtering Recency Windows ─────────────────────────────
    highlight_recency_days: int = 30       # 1 month — reject if YouTube highlights/clips found
    twitch_link_recency_days: int = 7      # 1 week  — reject if Twitch-panel YT link is active
    channel_search_recency_days: int = 30  # 1 month — reject if any YT channel by name is active

    # ── Misc ──────────────────────────────────────────────────
    debug: bool = False


_settings: Settings | None = None


def get_settings() -> Settings:
    """Return a singleton of the application settings."""
    global _settings
    if _settings is None:
        _settings = Settings()
    return _settings
