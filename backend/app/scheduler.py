"""APScheduler jobs: daily scan, domain check, log cleanup.
Reschedules itself when settings change (call reload())."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from apscheduler.schedulers.background import BackgroundScheduler

from . import log
from .db import settings_store as st
from .db.models import LogEntry
from .db.session import session_scope
from .pipeline import scanner
from .scraper.domain import ensure_current_domain

scheduler = BackgroundScheduler(job_defaults={
    "coalesce": True, "max_instances": 1, "misfire_grace_time": 3600})


def _parse_hhmm(value: str, fallback: str) -> tuple[int, int]:
    try:
        h, m = value.strip().split(":")
        return int(h) % 24, int(m) % 60
    except Exception:
        h, m = fallback.split(":")
        return int(h), int(m)


def job_daily_scan() -> None:
    try:
        scanner.scan_new_movies()
    except Exception as e:
        log.error(f"Daily scan job crashed: {e}", exc=e)


def job_domain_check() -> None:
    try:
        with session_scope() as session:
            ensure_current_domain(session)
    except Exception as e:
        log.error(f"Domain check job crashed: {e}", exc=e)


def job_radarr_sync() -> None:
    try:
        scanner.sync_radarr()
    except Exception as e:
        log.error(f"Radarr sync job crashed: {e}", exc=e)


def job_clean_logs() -> None:
    try:
        with session_scope() as session:
            days = st.get_int(session, "log_retention_days") or 30
            cutoff = datetime.now(timezone.utc) - timedelta(days=days)
            n = (session.query(LogEntry)
                 .filter(LogEntry.created_at < cutoff).delete())
            log.info(f"Log cleanup: removed {n} entries older than {days}d")
    except Exception as e:
        log.error(f"Log cleanup crashed: {e}", exc=e)


def reload() -> None:
    """(Re-)register cron jobs from current settings."""
    for job_id in ("daily_scan", "domain_check", "radarr_sync", "clean_logs"):
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    with session_scope() as session:
        if st.get_bool(session, "daily_scan_enabled"):
            h, m = _parse_hhmm(st.get(session, "daily_scan_time") or "06:00", "06:00")
            scheduler.add_job(job_daily_scan, "cron", hour=h, minute=m,
                              id="daily_scan")
            log.info(f"Scheduled daily scan at {h:02d}:{m:02d}")
        if st.get_bool(session, "domain_check_enabled"):
            h, m = _parse_hhmm(st.get(session, "domain_check_time") or "05:30", "05:30")
            scheduler.add_job(job_domain_check, "cron", hour=h, minute=m,
                              id="domain_check")
            log.info(f"Scheduled domain check at {h:02d}:{m:02d}")
        if st.get_bool(session, "radarr_sync_enabled"):
            h, m = _parse_hhmm(st.get(session, "radarr_sync_time") or "05:45", "05:45")
            scheduler.add_job(job_radarr_sync, "cron", hour=h, minute=m,
                              id="radarr_sync")
            log.info(f"Scheduled Radarr sync at {h:02d}:{m:02d}")
    scheduler.add_job(job_clean_logs, "cron", hour=3, minute=0, id="clean_logs")


def run_in_background(fn, job_id: str, **kwargs) -> bool:
    """Fire-and-forget job (used for manual scans from the UI)."""
    if scheduler.get_job(job_id):
        return False
    scheduler.add_job(fn, id=job_id, kwargs=kwargs,
                      next_run_time=datetime.now(timezone.utc))
    return True


def start() -> None:
    reload()
    scheduler.start()
