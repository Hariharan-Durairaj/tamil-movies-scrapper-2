"""FastAPI app entry point."""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles

from . import log, scheduler
from .api.routes import router
from .config import env
from .db import settings_store as st
from .db.session import init_db, session_scope

FRONTEND_DIR = Path(__file__).resolve().parent.parent.parent / "frontend"


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db()
    with session_scope() as s:
        st.ensure_defaults(s)
    scheduler.start()
    log.info("Movie Automator v2 started")
    yield
    scheduler.scheduler.shutdown(wait=False)


app = FastAPI(title="Tamil Movie Automator", lifespan=lifespan)
app.include_router(router)
app.mount("/posters", StaticFiles(directory=env.posters_dir), name="posters")
app.mount("/", StaticFiles(directory=FRONTEND_DIR, html=True), name="frontend")


def run() -> None:
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=env.port)


if __name__ == "__main__":
    run()
