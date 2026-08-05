"""
StreamScout & GigHunt — Celery Application Factory
=====================================================
Configures Celery with a **filesystem broker** by default (no Redis
required).  If ``REDIS_URL`` is set in ``.env``, it will use Redis
instead for better performance.

Beat schedule runs the daily Twitch scan automatically.
"""

from __future__ import annotations

import os
from pathlib import Path

from celery import Celery
from celery.schedules import crontab

from config import get_settings

_settings = get_settings()

# ── Broker selection ─────────────────────────────────────────
# Use Redis if configured, otherwise fall back to filesystem broker
# (zero-install, works out of the box on any OS).

if _settings.redis_url:
    _broker_url = _settings.redis_url
    _backend_url = _settings.redis_url
else:
    # Filesystem broker — create required directories
    _broker_dir = Path("./celery_broker")
    _broker_out = _broker_dir / "out"
    _broker_processed = _broker_dir / "processed"
    _results_dir = Path("./celery_results")

    _broker_out.mkdir(parents=True, exist_ok=True)
    _broker_processed.mkdir(parents=True, exist_ok=True)
    _results_dir.mkdir(parents=True, exist_ok=True)

    _broker_url = "filesystem://"
    _backend_url = f"file:///{_results_dir.resolve().as_posix()}"

# ── Celery App ───────────────────────────────────────────────

celery_app = Celery(
    "streamscout",
    broker=_broker_url,
    backend=_backend_url,
)

# Filesystem broker transport options (ignored if using Redis)
if not _settings.redis_url:
    celery_app.conf.broker_transport_options = {
        "data_folder_in": str(_broker_out.resolve()),
        "data_folder_out": str(_broker_out.resolve()),
        "data_folder_processed": str(_broker_processed.resolve()),
    }

celery_app.conf.update(
    # Serialisation
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",

    # Timezone
    timezone="UTC",
    enable_utc=True,

    # Task discovery
    imports=["tasks"],

    # Concurrency (keep low for scraping to avoid bans)
    worker_concurrency=2,
    worker_prefetch_multiplier=1,

    # Task limits
    task_soft_time_limit=3600,   # 1 hour soft limit
    task_time_limit=7200,        # 2 hour hard limit

    # Retry policy
    task_acks_late=True,
    task_reject_on_worker_lost=True,
)

# ── Beat Schedule (Periodic Tasks) ──────────────────────────

celery_app.conf.beat_schedule = {
    "daily-twitch-scan": {
        "task": "tasks.run_daily_twitch_scan",
        "schedule": crontab(hour=3, minute=0),  # 03:00 UTC daily
        "kwargs": {"max_pages": 20},
    },
}
