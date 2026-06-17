"""REST API. No auth — LAN-only deployment by user's choice."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel
from sqlalchemy import func, or_

from .. import log, scheduler
from ..db import settings_store as st
from ..db.models import LogEntry, Movie, MovieStatus, MovieTorrent
from ..db.session import session_scope
from ..metadata.engine import MatchEngine
from ..pipeline import processor, scanner
from ..scraper.domain import ensure_current_domain

router = APIRouter(prefix="/api")


# ── serialization ────────────────────────────────────────────────────────

def poster_url(m: Movie) -> str | None:
    """Local poster URL with a cache-busting version. The on-disk filename is
    stable ('<id>.webp'), so when a match is corrected the file is overwritten
    but the URL would stay the same and the browser would keep showing the old
    cached image. Appending ?v=<updated_at> changes the URL on every edit."""
    if not m.poster_path:
        return None
    ver = int(m.updated_at.timestamp()) if m.updated_at else 0
    return f"/posters/{m.poster_path}?v={ver}"


def movie_dict(m: Movie, with_torrents: bool = False) -> dict:
    d = {
        "id": m.id, "title": m.title, "year": m.year,
        "forum_title": m.forum_title, "forum_url": m.forum_url,
        "source": m.source, "status": m.status,
        "rejection_reason": m.rejection_reason,
        "matched_title": m.matched_title,
        "original_language": m.original_language,
        "is_tamil_original": m.is_tamil_original,
        "imdb_id": m.imdb_id, "tmdb_id": m.tmdb_id,
        "rating": m.rating, "rating_source": m.rating_source,
        "match_confidence": m.match_confidence,
        "poster": poster_url(m),
        "forum_languages": m.forum_languages,
        "downloaded_quality": m.downloaded_quality,
        "added_to_radarr": m.added_to_radarr,
        "added_to_qbittorrent": m.added_to_qbittorrent,
        "radarr_skip_reason": m.radarr_skip_reason,
        "created_at": m.created_at.isoformat() if m.created_at else None,
        "updated_at": m.updated_at.isoformat() if m.updated_at else None,
    }
    if with_torrents:
        d["torrents"] = [{
            "id": t.id, "name": t.name, "quality": t.quality,
            "codec": t.codec, "rip_type": t.rip_type,
            "file_size": t.file_size, "is_magnet": t.is_magnet,
            "languages": t.languages, "source_format": t.source_format,
        } for t in m.torrents]
        d["candidates"] = m.match_candidates or []
    return d


# ── dashboard ────────────────────────────────────────────────────────────

@router.get("/stats")
def stats():
    with session_scope() as s:
        by_status = dict(s.query(Movie.status, func.count(Movie.id))
                         .group_by(Movie.status).all())
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "tamil_originals": s.query(Movie).filter(
                Movie.is_tamil_original.is_(True)).count(),
            "needs_review": by_status.get(MovieStatus.NEEDS_REVIEW, 0),
            "sent": by_status.get(MovieStatus.SENT, 0),
            "current_domain": st.get(s, "current_domain"),
            "full_scan": scanner.full_scan_status(),
        }


# ── movies / library ─────────────────────────────────────────────────────

@router.get("/movies")
def list_movies(status: str | None = None, tamil: bool | None = None,
                q: str | None = None, source: str | None = None,
                page: int = 1, per_page: int = Query(40, le=200),
                sort: str = "created_desc"):
    with session_scope() as s:
        query = s.query(Movie)
        if status:
            query = query.filter(Movie.status.in_(status.split(",")))
        if tamil is not None:
            query = query.filter(Movie.is_tamil_original.is_(tamil))
        if source:
            query = query.filter(Movie.source == source)
        if q:
            like = f"%{q}%"
            query = query.filter(or_(Movie.title.ilike(like),
                                     Movie.matched_title.ilike(like)))
        total = query.count()
        order = {
            "created_desc": Movie.created_at.desc(),
            "rating_desc": Movie.rating.desc().nullslast(),
            "year_desc": Movie.year.desc().nullslast(),
            "title_asc": Movie.title.asc(),
        }.get(sort, Movie.created_at.desc())
        movies = (query.order_by(order)
                  .offset((page - 1) * per_page).limit(per_page).all())
        return {"total": total, "page": page, "per_page": per_page,
                "movies": [movie_dict(m) for m in movies]}


@router.get("/movies/{movie_id}")
def get_movie(movie_id: int):
    with session_scope() as s:
        m = s.get(Movie, movie_id)
        if not m:
            raise HTTPException(404)
        return movie_dict(m, with_torrents=True)


@router.delete("/movies/{movie_id}")
def delete_movie(movie_id: int):
    with session_scope() as s:
        m = s.get(Movie, movie_id)
        if not m:
            raise HTTPException(404)
        s.delete(m)
        return {"ok": True}


class DownloadBody(BaseModel):
    torrent_id: int | None = None


@router.post("/movies/{movie_id}/download")
def download_movie(movie_id: int, body: DownloadBody):
    """Manual send — bypasses the rating threshold."""
    with session_scope() as s:
        m = s.get(Movie, movie_id)
        if not m:
            raise HTTPException(404)
        # library entries may not have torrents fetched yet
        if not m.torrents and m.forum_url:
            try:
                torrents, _ = processor.fetch_post(m)
                processor.store_torrents(s, m, torrents)
            except Exception as e:
                raise HTTPException(502, f"could not fetch torrents: {e}")
        return processor.download_movie(s, m, body.torrent_id)


class ReviewBody(BaseModel):
    candidate_idx: int


@router.post("/movies/{movie_id}/review")
def review_movie(movie_id: int, body: ReviewBody):
    with session_scope() as s:
        m = s.get(Movie, movie_id)
        if not m:
            raise HTTPException(404)
        return processor.apply_review_choice(s, m, body.candidate_idx)


class ImdbBody(BaseModel):
    imdb_id: str


@router.post("/movies/{movie_id}/set-imdb")
def set_imdb(movie_id: int, body: ImdbBody):
    """Manually pin a movie to an IMDb id, then re-enrich (tmdb id, rating,
    poster, language) and mark it as a human-confirmed match."""
    imdb_id = body.imdb_id.strip()
    if not imdb_id.startswith("tt"):
        raise HTTPException(400, "imdb_id must look like tt1234567")
    with session_scope() as s:
        m = s.get(Movie, movie_id)
        if not m:
            raise HTTPException(404)
        return processor.set_imdb_id(s, m, imdb_id)


@router.post("/movies/{movie_id}/rematch")
def rematch_movie(movie_id: int):
    """Re-run metadata matching (e.g. after fixing API keys)."""
    with session_scope() as s:
        m = s.get(Movie, movie_id)
        if not m:
            raise HTTPException(404)
        post_text = None
        try:
            _, post_text = processor.fetch_post(m)
        except Exception:
            pass
        result = MatchEngine(s).match(m.title, m.year,
                                      m.forum_languages, post_text)
        m.poster_path = None
        processor.apply_match(s, m, result)
        return movie_dict(m)


# ── scans ────────────────────────────────────────────────────────────────

@router.post("/scan")
def trigger_scan():
    started = scheduler.run_in_background(scanner.scan_new_movies, "manual_scan")
    return {"ok": started, "note": None if started else "scan already running"}


@router.post("/library/full-scan/start")
def full_scan_start(max_pages: int | None = None,
                    start_page: int | None = None,
                    end_page: int | None = None):
    """If start_page/end_page are given, scan that inclusive range (ignoring
    the saved checkpoint). Otherwise resume from the checkpoint."""
    started = scheduler.run_in_background(
        scanner.full_library_scan, "full_scan",
        max_pages_this_run=max_pages,
        start_page=start_page, end_page=end_page)
    return {"ok": started, "note": None if started else "already running"}


@router.post("/library/full-scan/stop")
def full_scan_stop():
    scanner.request_full_scan_stop()
    return {"ok": True}


@router.get("/library/full-scan/status")
def full_scan_status():
    return scanner.full_scan_status()


@router.post("/library/full-scan/reset")
def full_scan_reset():
    with session_scope() as s:
        st.set_value(s, "full_scan_last_page", "0")
    return {"ok": True}


# ── search ───────────────────────────────────────────────────────────────

@router.get("/search")
def search(q: str, page: int = 1, old: bool = False):
    """Default: fast JSON search.php API (priority posts first, paginated).
    old=true falls back to the legacy scraper that pre-filters torrent-less
    results."""
    if not q.strip():
        raise HTTPException(400, "empty query")
    if old:
        return {"mode": "old", "results": scanner.search_forum(q.strip()),
                "page": 1, "page_count": 1}
    return scanner.search_forum_api(q.strip(), page)


@router.get("/search/torrents")
def search_torrents(forum_url: str):
    """Lazy torrent fetch for one result in the new search UI."""
    return {"torrents": scanner.search_post_torrents(forum_url)}


class SearchDownloadBody(BaseModel):
    forum_url: str
    forum_title: str
    torrent_url: str


@router.post("/search/download")
def search_download(body: SearchDownloadBody):
    """User picked a torrent from search results: catalog + match + send."""
    from ..scraper.parse import parse_movie_title_year
    with session_scope() as s:
        parsed = parse_movie_title_year(body.forum_title)
        movie, created = processor.get_or_create_movie(
            s, parsed["title"], parsed["year"],
            body.forum_url, body.forum_title, "manual_search")
        try:
            torrents, post_text = processor.fetch_post(movie)
            processor.store_torrents(s, movie, torrents)
        except Exception as e:
            raise HTTPException(502, f"could not fetch post: {e}")
        if created or movie.status in (MovieStatus.DISCOVERED,):
            result = MatchEngine(s).match(movie.title, movie.year,
                                          movie.forum_languages, post_text)
            processor.apply_match(s, movie, result)
        chosen = next((t for t in movie.torrents
                       if t.torrent_url == body.torrent_url), None)
        return processor.download_movie(s, movie,
                                        chosen.id if chosen else None)


# ── settings ─────────────────────────────────────────────────────────────

@router.get("/settings")
def get_settings():
    with session_scope() as s:
        return st.all_settings(s)


@router.put("/settings")
def put_settings(values: dict[str, str]):
    with session_scope() as s:
        for key, value in values.items():
            st.set_value(s, key, str(value))
    scheduler.reload()
    log.info("Settings updated", keys=list(values.keys()))
    return {"ok": True}


# ── logs ─────────────────────────────────────────────────────────────────

@router.get("/logs")
def get_logs(level: str | None = None, limit: int = Query(200, le=1000)):
    with session_scope() as s:
        q = s.query(LogEntry).order_by(LogEntry.created_at.desc())
        if level:
            q = q.filter(LogEntry.level == level.upper())
        return [{"id": e.id, "level": e.level, "message": e.message,
                 "context": e.context,
                 "created_at": e.created_at.isoformat()}
                for e in q.limit(limit)]


# ── system ───────────────────────────────────────────────────────────────

@router.post("/system/domain-check")
def domain_check(force: bool = False):
    with session_scope() as s:
        domain = ensure_current_domain(s, force_search=force)
        return {"ok": domain is not None, "domain": domain}


@router.post("/system/reset-all")
def reset_all():
    """Delete all data (movies, torrents, logs, metadata cache, scan state,
    posters) but keep settings."""
    return processor.reset_all_data()


@router.post("/system/radarr-sync")
def radarr_sync():
    """Import Radarr's library into the DB (duplicate protection)."""
    return scanner.sync_radarr()


@router.post("/system/test-radarr")
def test_radarr():
    from ..integrations.radarr import RadarrClient
    with session_scope() as s:
        client = RadarrClient(st.get(s, "radarr_url") or "",
                              st.get(s, "radarr_api_key") or "")
        ok = client.test_connection()
        profiles = client.quality_profiles() if ok else []
        return {"ok": ok, "profiles": [{"id": p["id"], "name": p["name"]}
                                       for p in profiles]}


@router.post("/system/test-qbittorrent")
def test_qbittorrent():
    from ..integrations.qbittorrent import QBittorrentClient
    with session_scope() as s:
        client = QBittorrentClient(st.get(s, "qbittorrent_url") or "",
                                   st.get(s, "qbittorrent_username") or "",
                                   st.get(s, "qbittorrent_password") or "")
        return {"ok": client.test_connection()}


@router.get("/health")
def health():
    return {"ok": True}
