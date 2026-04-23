from __future__ import annotations

import uvicorn
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

from meshonator.api.app import app, registry
from meshonator.config.settings import get_settings
from meshonator.db.session import SessionLocal
from meshonator.sync.service import SyncService

settings = get_settings()
scheduler = BackgroundScheduler(timezone="UTC")


def scheduled_sync() -> None:
    with SessionLocal() as db:
        SyncService(db, registry).sync_all(quick=True)


if settings.scheduler_enabled:
    scheduler.add_job(
        scheduled_sync,
        CronTrigger.from_crontab(settings.scheduler_sync_cron),
        id="periodic_quick_sync",
        replace_existing=True,
    )
    scheduler.start()


if __name__ == "__main__":
    uvicorn.run("meshonator.api.app:app", host=settings.app_host, port=settings.app_port, reload=settings.app_env == "dev")
