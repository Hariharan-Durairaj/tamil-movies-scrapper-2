"""Logging to stdout + DB (the UI Logs page reads the DB).

Uses its own short-lived session so it can be called from anywhere,
including inside another session's transaction or from scheduler threads.
"""
import traceback

from .db.models import LogEntry
from .db.session import SessionLocal

LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


def log(level: str, message: str, context: dict | None = None,
        exc: BaseException | None = None) -> None:
    level = level.upper() if level.upper() in LEVELS else "INFO"
    print(f"[{level}] {message}" + (f" | {context}" if context else ""), flush=True)

    ctx = dict(context or {})
    if exc is not None:
        ctx["exception"] = "".join(
            traceback.format_exception(type(exc), exc, exc.__traceback__))[-4000:]

    try:
        s = SessionLocal()
        try:
            s.add(LogEntry(level=level, message=message[:8000], context=ctx or None))
            s.commit()
        finally:
            s.close()
    except Exception as e:  # never let logging break the pipeline
        print(f"[LOG-FAIL] {e}", flush=True)


def debug(msg, **ctx): log("DEBUG", msg, ctx or None)
def info(msg, **ctx): log("INFO", msg, ctx or None)
def warning(msg, **ctx): log("WARNING", msg, ctx or None)
def error(msg, exc=None, **ctx): log("ERROR", msg, ctx or None, exc=exc)
