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
    # No result backend — all tasks are fire-and-forget scheduled jobs.
    # Storing results in Redis was the primary cause of hitting Upstash request limits.
    include=["app.tasks.high_intent", "app.tasks.hubspot_sync", "app.tasks.email_validation"],
)

celery_app.conf.update(
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    broker_connection_retry_on_startup=True,
    # Required for rediss:// (TLS) connections — Upstash and Railway Redis both use TLS
    broker_use_ssl={"ssl_cert_reqs": ssl.CERT_NONE},
    # Don't store task results — saves ~4 Redis writes per task execution
    task_ignore_result=True,
    # Don't broadcast task events — reduces Redis pub/sub traffic significantly
    worker_send_task_events=False,
    task_send_sent_event=False,
    # Beat schedule — runs inside the combined worker+beat process
    beat_schedule={
        "scan-high-intent": {
            "task": "app.tasks.high_intent.scan_high_intent",
            "schedule": crontab(minute="*/15"),  # every 15 minutes
        },
        "sync-to-hubspot": {
            "task": "app.tasks.hubspot_sync.sync_to_hubspot",
            "schedule": crontab(minute="*/15"),  # every 15 minutes (was 5 — not time-sensitive)
        },
        "validate-emails": {
            "task": "app.tasks.email_validation.validate_emails",
            "schedule": crontab(minute="*/30"),  # every 30 minutes
        },
    },
)
