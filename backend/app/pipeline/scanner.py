"""Scanning jobs: daily new-movie scan, full forum library scan (resumable),
and forum search with torrent filtering."""
from __future__ import annotations

import time

from sqlalchemy.orm import Session

from .. import log
from ..db import settings_store as st
from ..db.models import Movie, MovieStatus, TaskState
from ..db.session import session_scope
from ..scraper import forum
from ..scraper.domain import ensure_current_domain
from ..scraper.http import fetch_soup
from ..scraper.parse import parse_movie_title_year
from ..scraper.torrents import extract_torrents
from . import processor


# ── task_state helpers ───────────────────────────────────────────────────

def _state_get(session: Session, key: str, default: str = "") -> str:
    row = session.get(TaskState, key)
    return row.value if row and row.value is not None else default


def _state_set(session: Session, key: str, value: str) -> None:
    row = session.get(TaskState, key)
    if row:
        row.value = value
    else:
        session.add(TaskState(key=key, value=value))
    session.commit()


def _with_domain_retry(session: Session, fn):
    """Run fn(); on connection-level failure, rediscover the domain and retry
    once. This is the automatic recovery for TLD changes."""
    try:
        return fn()
    except Exception as e:
        log.warning(f"Forum request failed ({e}) — checking domain")
        domain = ensure_current_domain(session)
        session.commit()
        if not domain:
            raise
        return fn()


# ── daily scan ───────────────────────────────────────────────────────────

def scan_new_movies(max_pages: int | None = None,
                    max_links: int | None = None) -> list[dict]:
    """Scan newest forum pages, process each unseen topic through the full
    pipeline. Stops after N consecutive already-seen movies."""
    results: list[dict] = []
    with session_scope() as session:
        max_pages = max_pages or st.get_int(session, "scan_pages") or 3
        max_links = max_links or st.get_int(session, "scan_max_links") or 50
        dup_stop = st.get_int(session, "duplicate_stop_count") or 5
        auto_dl = st.get_bool(session, "auto_download")
        forum_base = st.forum_url(session)

        log.info("Daily scan started", pages=max_pages, max_links=max_links,
                 auto_download=auto_dl)
        duplicates = 0
        processed = 0

        for page in range(1, max_pages + 1):
            url = forum.page_url(forum_base, page)
            try:
                topics = _with_domain_retry(session, lambda u=url: forum.list_topics(u))
            except Exception as e:
                log.error(f"Scan: page {page} failed: {e}", exc=e)
                continue

            for topic in topics:
                if processed >= max_links:
                    break
                parsed = parse_movie_title_year(topic["text"])
                from sqlalchemy import func
                q = session.query(Movie).filter(
                    func.lower(Movie.title) == parsed["title"].lower())
                q = (q.filter(Movie.year == parsed["year"])
                     if parsed["year"] is not None else q.filter(Movie.year.is_(None)))
                if q.count() > 0:
                    duplicates += 1
                    if duplicates >= dup_stop:
                        log.info(f"Scan: {duplicates} consecutive duplicates — stopping")
                        session.commit()
                        return results
                    continue

                duplicates = 0
                processed += 1
                try:
                    r = processor.process_topic(session, topic,
                                                source="auto_scan",
                                                auto_download=auto_dl)
                    results.append(r)
                    session.commit()
                except Exception as e:
                    session.rollback()
                    log.error(f"Scan: processing '{topic['text'][:60]}' crashed: {e}", exc=e)
            if processed >= max_links:
                break

        log.info("Daily scan complete", processed=processed, results=len(results))
    return results


# ── full forum library scan (resumable) ──────────────────────────────────

def full_scan_status() -> dict:
    with session_scope() as session:
        forum_base = st.forum_url(session)
        return {
            "running": _state_get(session, "full_scan_running") == "1",
            "last_page": int(st.get(session, "full_scan_last_page") or 0),
            "total_pages": int(_state_get(session, "full_scan_total_pages", "0") or 0),
            "cataloged": int(_state_get(session, "full_scan_cataloged", "0") or 0),
            "forum_url": forum_base,
        }


def request_full_scan_stop() -> None:
    with session_scope() as session:
        _state_set(session, "full_scan_stop", "1")


def full_library_scan(max_pages_this_run: int | None = None) -> dict:
    """Walk every forum page (resuming from the checkpoint) and catalog all
    movies. Never downloads. Politeness delay between posts."""
    with session_scope() as session:
        if _state_get(session, "full_scan_running") == "1":
            return {"ok": False, "error": "already running"}
        _state_set(session, "full_scan_running", "1")
        _state_set(session, "full_scan_stop", "0")

    cataloged = 0
    try:
        with session_scope() as session:
            forum_base = st.forum_url(session)
            delay = st.get_float(session, "full_scan_delay_seconds") or 3.0
            start_page = (st.get_int(session, "full_scan_last_page") or 0) + 1
            total = _with_domain_retry(session,
                                       lambda: forum.total_pages(forum_base))
            _state_set(session, "full_scan_total_pages", str(total))
            log.info(f"Full scan: pages {start_page}..{total}")

        end_page = total
        if max_pages_this_run:
            end_page = min(total, start_page + max_pages_this_run - 1)

        for page in range(start_page, end_page + 1):
            with session_scope() as session:
                if _state_get(session, "full_scan_stop") == "1":
                    log.info("Full scan: stop requested")
                    break
                url = forum.page_url(st.forum_url(session), page)
                try:
                    topics = _with_domain_retry(session,
                                                lambda u=url: forum.list_topics(u))
                except Exception as e:
                    log.error(f"Full scan: page {page} failed: {e}", exc=e)
                    continue

                for topic in topics:
                    try:
                        r = processor.catalog_topic(session, topic)
                        if r["status"] == "cataloged":
                            cataloged += 1
                            _state_set(session, "full_scan_cataloged",
                                       str(int(_state_get(session, "full_scan_cataloged", "0") or 0) + 1))
                            time.sleep(delay)
                        session.commit()
                    except Exception as e:
                        session.rollback()
                        log.error(f"Full scan: '{topic['text'][:60]}' crashed: {e}", exc=e)

                st.set_value(session, "full_scan_last_page", str(page))
                session.commit()
                log.info(f"Full scan: page {page}/{total} done")
    finally:
        with session_scope() as session:
            _state_set(session, "full_scan_running", "0")

    log.info(f"Full scan finished: {cataloged} new movies cataloged")
    return {"ok": True, "cataloged": cataloged}


# ── Radarr library sync ──────────────────────────────────────────────────

def sync_radarr() -> dict:
    """Import Radarr's library into the DB so movies you already have are
    never downloaded a second time (the title/year dedupe in the scan and
    the tmdb/imdb duplicate guard both hit these rows)."""
    from ..integrations.radarr import RadarrClient

    added = updated = 0
    with session_scope() as session:
        url = st.get(session, "radarr_url") or ""
        if not url:
            return {"ok": False, "error": "radarr not configured"}
        radarr = RadarrClient(url, st.get(session, "radarr_api_key") or "")
        radarr_movies = radarr.get_movies()
        if not radarr_movies:
            return {"ok": False, "error": "Radarr returned no movies (check URL/API key)"}

        from sqlalchemy import func
        for rm in radarr_movies:
            title = rm.get("title") or ""
            year = rm.get("year")
            tmdb_id = rm.get("tmdbId")
            imdb_id = rm.get("imdbId")
            lang = ((rm.get("originalLanguage") or {}).get("name") or "")

            movie = None
            if tmdb_id:
                movie = (session.query(Movie)
                         .filter(Movie.tmdb_id == tmdb_id).one_or_none())
            if movie is None and title:
                q = session.query(Movie).filter(
                    func.lower(Movie.title) == title.lower())
                q = (q.filter(Movie.year == year) if year is not None
                     else q.filter(Movie.year.is_(None)))
                movie = q.one_or_none()

            rating = None
            ratings = rm.get("ratings") or {}
            if isinstance(ratings.get("imdb"), dict):
                rating = ratings["imdb"].get("value")

            if movie:
                movie.added_to_radarr = True
                movie.tmdb_id = movie.tmdb_id or tmdb_id
                movie.imdb_id = movie.imdb_id or imdb_id
                if movie.is_tamil_original is None and lang:
                    movie.is_tamil_original = (lang == "Tamil")
                updated += 1
            else:
                session.add(Movie(
                    title=title, year=year, source="radarr_sync",
                    status=MovieStatus.IN_RADARR,
                    matched_title=title, tmdb_id=tmdb_id, imdb_id=imdb_id,
                    rating=rating, rating_source="radarr" if rating else None,
                    original_language={"Tamil": "ta", "Telugu": "te", "Hindi": "hi",
                                       "Malayalam": "ml", "Kannada": "kn",
                                       "English": "en"}.get(lang),
                    is_tamil_original=(lang == "Tamil") if lang else None,
                    added_to_radarr=True,
                    rejection_reason=("already in Radarr (downloaded)"
                                      if rm.get("hasFile")
                                      else "already in Radarr (monitored)"),
                ))
                added += 1
        session.commit()

    processor.invalidate_radarr_cache()
    log.info(f"Radarr sync: {added} imported, {updated} linked",
             radarr_total=len(radarr_movies))
    return {"ok": True, "imported": added, "linked": updated,
            "radarr_total": len(radarr_movies)}


# ── forum search (filters out torrent-less results) ──────────────────────

def search_forum(query: str, max_check: int = 12) -> list[dict]:
    """Search the forum and keep only results whose post actually contains
    torrents. Returns each with its parsed qualities so the UI can offer
    a direct download choice."""
    out: list[dict] = []
    with session_scope() as session:
        url = st.search_url(session, query)
        results = _with_domain_retry(session, lambda: forum.search_results(url))

        for r in results[:max_check]:
            try:
                soup = fetch_soup(r["href"])
                torrents = extract_torrents(soup, r["href"])
            except Exception as e:
                log.debug(f"Search: skipping {r['href']}: {e}")
                continue
            if not torrents:
                continue
            parsed = parse_movie_title_year(r["text"])
            out.append({
                "title": parsed["title"],
                "year": parsed["year"],
                "forum_title": r["text"],
                "forum_url": r["href"],
                "torrents": [{k: t[k] for k in
                              ("name", "torrent_url", "is_magnet", "quality",
                               "codec", "rip_type", "file_size", "languages")}
                             for t in torrents],
            })
    log.info(f"Search '{query}': {len(out)} results with torrents")
    return out
