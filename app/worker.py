"""
Celery application instance.

Run worker+beat (combined, single process):
  venv\\Scripts\\celery.exe -A app.worker worker --beat -l info

The built-in PersistentScheduler stores beat state in celerybeat-schedule
(local file). Fine for a single combined worker+beat process on Railway.
"""

import ssl

from celery import Celery
from celery.schedules import crontab

from app.config import settings

celery_app = Celery(
    "wcp_outbound",
    broker=settings.redis_url,
    backend=settings.redis_url,
    include=["app.tasks.high_intent", "app.tasks.hubspot_sync"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # Required for rediss:// (TLS) connections — Upstash and Railway Redis both use TLS
    redis_backend_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE},
    broker_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE},
    # Beat schedule — runs inside the combined worker+beat process
    beat_schedule={
        "scan-high-intent": {
            "task": "app.tasks.high_intent.scan_high_intent",
            "schedule": crontab(minute="*/15"),  # every 15 minutes
        },
        "sync-to-hubspot": {
            "task": "app.tasks.hubspot_sync.sync_to_hubspot",
            "schedule": crontab(minute="*/5"),  # every 5 minutes
        },
    },
)
