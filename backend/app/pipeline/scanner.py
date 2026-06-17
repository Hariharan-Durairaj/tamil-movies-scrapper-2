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
from ..metadata.posters import save_poster
from ..scraper.http import fetch_json, fetch_soup
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


def full_library_scan(max_pages_this_run: int | None = None,
                      start_page: int | None = None,
                      end_page: int | None = None) -> dict:
    """Walk forum pages and catalog all movies. Never downloads. Politeness
    delay between posts.

    Without start_page/end_page it resumes from the saved checkpoint and runs
    to the last page. With an explicit range it scans start_page..end_page
    inclusive (the checkpoint is still advanced so the dashboard reflects it)."""
    explicit_range = start_page is not None
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
            total = _with_domain_retry(session,
                                       lambda: forum.total_pages(forum_base))
            _state_set(session, "full_scan_total_pages", str(total))

            if explicit_range:
                first_page = max(1, start_page)
                last_page = end_page if end_page else total
                last_page = min(last_page, total) if total else last_page
            else:
                first_page = (st.get_int(session, "full_scan_last_page") or 0) + 1
                last_page = total
            log.info(f"Full scan: pages {first_page}..{last_page}"
                     + (" (explicit range)" if explicit_range else ""))

        run_start, run_end = first_page, last_page
        if max_pages_this_run:
            run_end = min(run_end, run_start + max_pages_this_run - 1)

        for page in range(run_start, run_end + 1):
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

        # Only sync movies stored under the configured root folder. Radarr can
        # have several root folders; the user wants just this one.
        root = (st.get(session, "radarr_root_folder") or "").strip().rstrip("/")
        skipped_path = 0
        if root:
            def _under_root(rm: dict) -> bool:
                p = (rm.get("rootFolderPath") or rm.get("path") or "").rstrip("/")
                return p == root or p.startswith(root + "/")
            filtered = [rm for rm in radarr_movies if _under_root(rm)]
            skipped_path = len(radarr_movies) - len(filtered)
            radarr_movies = filtered

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
                movie = Movie(
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
                )
                session.add(movie)
                added += 1

            # Posters: Radarr-synced rows had no image. Pull the poster Radarr
            # already knows about (remoteUrl → TMDB) so the library shows art.
            if not movie.poster_path:
                poster_url = _radarr_poster_url(rm)
                if poster_url:
                    session.flush()  # need movie.id for the filename
                    movie.poster_path = save_poster(poster_url, movie.id)
        session.commit()

    processor.invalidate_radarr_cache()
    log.info(f"Radarr sync: {added} imported, {updated} linked, "
             f"{skipped_path} outside root folder",
             radarr_total=len(radarr_movies))
    return {"ok": True, "imported": added, "linked": updated,
            "skipped_outside_root": skipped_path,
            "radarr_total": len(radarr_movies)}


def _radarr_poster_url(rm: dict) -> str | None:
    """Best poster URL from a Radarr movie's images array."""
    for img in rm.get("images") or []:
        if img.get("coverType") == "poster":
            url = img.get("remoteUrl") or img.get("url")
            if url and url.startswith("http"):
                return url
    return None


# ── forum search (new JSON API, priority posts first) ────────────────────

def search_forum_api(query: str, page: int = 1) -> dict:
    """Direct search.php JSON API. Fast: no per-post scraping. Returns results
    ordered priority-first (moderator releases), with pagination info so the
    UI can 'load more'. Torrents are fetched lazily per result on demand."""
    with session_scope() as session:
        url = st.search_api_url(session, query, page)
        data = _with_domain_retry(session, lambda: fetch_json(url)) or {}
        results = []
        for r in data.get("results", []):
            tid = r.get("tid")
            if not tid:
                continue
            parsed = parse_movie_title_year(r.get("title") or "")
            results.append({
                "tid": tid,
                "title": parsed["title"],
                "year": parsed["year"],
                "forum_title": r.get("title"),
                "forum_url": st.topic_url(session, tid),
                "priority": bool(r.get("priority")),
                "author": r.get("author"),
            })
        # priority (moderator) posts first, otherwise keep API order
        results.sort(key=lambda x: (not x["priority"],))
        log.info(f"Search API '{query}' p{page}: {len(results)} results")
        return {
            "mode": "api",
            "results": results,
            "page": data.get("page", page),
            "page_count": data.get("page_count", 1),
            "total": data.get("total", len(results)),
        }


def search_post_torrents(forum_url: str) -> list[dict]:
    """Fetch one post and return its torrents (empty if the post has none).
    Used by the new search UI to lazily reveal download options."""
    with session_scope() as session:
        try:
            soup = _with_domain_retry(session, lambda: fetch_soup(forum_url))
            torrents = extract_torrents(soup, forum_url)
        except Exception as e:
            log.debug(f"Search torrents fetch failed {forum_url}: {e}")
            return []
        return [{k: t[k] for k in
                 ("name", "torrent_url", "is_magnet", "quality",
                  "codec", "rip_type", "file_size", "languages")}
                for t in torrents]


# ── forum search (old scraper fallback, filters torrent-less results) ─────

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
