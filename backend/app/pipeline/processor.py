"""Movie pipeline — explicit state machine.

discovered → matched | needs_review | unmatched
matched    → qualified | rejected(below_threshold)
qualified  → sent | failed
(library scans stop at the metadata stage with status=library)

Every transition is logged. Rejected / needs_review movies stay in the DB
for one-click override from the UI.
"""
from __future__ import annotations

import os
import re

from sqlalchemy import func
from sqlalchemy.orm import Session

from .. import log
from ..config import env
from ..db import settings_store as st
from ..db.models import Movie, MovieStatus, MovieTorrent
from ..integrations.qbittorrent import QBittorrentClient
from ..integrations.radarr import RadarrClient
from ..metadata.engine import MatchEngine
from ..metadata.posters import save_poster
from ..scraper.http import download_file, fetch_soup
from ..scraper.parse import detect_languages, file_size_gb, parse_movie_title_year
from ..scraper.torrents import extract_torrents

QUALITY_RANK = {"1080p": 0, "FHD": 0, "720p": 1, "HD": 1,
                "2160p": 2, "4K": 2, "UHD": 2, "480p": 3}

# ── Radarr duplicate guard ───────────────────────────────────────────────
# Cached index of Radarr's library so the auto-scan never downloads a movie
# Radarr already has. Refreshed every 10 minutes or after a sync.
_radarr_cache: dict = {"at": 0.0, "by_tmdb": {}, "by_imdb": {}}


def invalidate_radarr_cache() -> None:
    _radarr_cache["at"] = 0.0


def radarr_lookup(session: Session, movie: Movie) -> dict | None:
    """Return the Radarr movie dict if this movie is already in Radarr."""
    import time as _t
    url = st.get(session, "radarr_url") or ""
    if not url or not (movie.tmdb_id or movie.imdb_id):
        return None
    if _t.time() - _radarr_cache["at"] > 600:
        movies = RadarrClient(url, st.get(session, "radarr_api_key") or "").get_movies()
        _radarr_cache["by_tmdb"] = {m["tmdbId"]: m for m in movies if m.get("tmdbId")}
        _radarr_cache["by_imdb"] = {(m.get("imdbId") or "").lower(): m
                                    for m in movies if m.get("imdbId")}
        _radarr_cache["at"] = _t.time()
    return (_radarr_cache["by_tmdb"].get(movie.tmdb_id)
            or _radarr_cache["by_imdb"].get((movie.imdb_id or "").lower()))


# ── helpers ──────────────────────────────────────────────────────────────

def get_or_create_movie(session: Session, title: str, year: int | None,
                        forum_url: str, forum_title: str,
                        source: str) -> tuple[Movie, bool]:
    """Returns (movie, created)."""
    q = session.query(Movie).filter(func.lower(Movie.title) == title.lower())
    q = q.filter(Movie.year == year) if year is not None else q.filter(Movie.year.is_(None))
    movie = q.one_or_none()
    if movie:
        return movie, False
    movie = Movie(title=title, year=year, forum_url=forum_url,
                  forum_title=forum_title, source=source,
                  forum_languages=detect_languages(forum_title),
                  status=MovieStatus.DISCOVERED)
    session.add(movie)
    session.flush()
    return movie, True


def fetch_post(movie: Movie) -> tuple[list[dict], str]:
    """Fetch the forum post once → (torrents, post_text)."""
    soup = fetch_soup(movie.forum_url)
    torrents = extract_torrents(soup, movie.forum_url)
    post = soup.select_one("div.ipsType_richText") or soup
    post_text = post.get_text(" ", strip=True)[:8000]
    return torrents, post_text


def store_torrents(session: Session, movie: Movie, torrents: list[dict]) -> None:
    existing = {t.torrent_url for t in movie.torrents}
    for t in torrents:
        if t["torrent_url"] in existing:
            continue
        session.add(MovieTorrent(
            movie_id=movie.id, name=t["name"], torrent_url=t["torrent_url"],
            is_magnet=t["is_magnet"], source_format=t["source_format"],
            quality=t["quality"], codec=t["codec"], rip_type=t["rip_type"],
            file_size=t["file_size"], languages=t["languages"]))
    session.flush()


def apply_match(session: Session, movie: Movie, result: dict) -> None:
    """Write a MatchEngine result onto the movie row (incl. poster)."""
    movie.match_candidates = [
        {k: v for k, v in c.items() if not k.startswith("_") or k == "_score"}
        for c in result["candidates"]]
    best = result["best"]
    if result["status"] == "unmatched" or not best:
        movie.status = MovieStatus.UNMATCHED
        return

    movie.matched_title = best.get("title")
    movie.original_language = best.get("original_language")
    movie.is_tamil_original = (best.get("original_language") == "ta") if best.get("original_language") else None
    movie.imdb_id = best.get("imdb_id")
    movie.tmdb_id = best.get("tmdb_id")
    movie.rating = best.get("rating")
    movie.rating_source = best.get("rating_source")
    movie.match_confidence = best.get("_score")
    movie.status = (MovieStatus.MATCHED if result["status"] == "matched"
                    else MovieStatus.NEEDS_REVIEW)

    if best.get("poster_url") and not movie.poster_path:
        movie.poster_path = save_poster(best["poster_url"], movie.id)


# ── torrent selection ────────────────────────────────────────────────────

def select_torrent(session: Session, torrents: list[MovieTorrent]) -> MovieTorrent | None:
    """Pick the best torrent: preferred quality (1080p) first, then preferred
    codec, then smallest size within the optional cap."""
    if not torrents:
        return None
    pref_q = (st.get(session, "preferred_quality") or "1080p").lower()
    pref_c = (st.get(session, "preferred_codec") or "").lower()
    max_gb = st.get_float(session, "max_size_gb")

    def keyfn(t: MovieTorrent):
        q = (t.quality or "").lower()
        c = (t.codec or "").lower()
        q_exact = 0 if q == pref_q else 1
        q_rank = QUALITY_RANK.get(t.quality or "", 9)
        c_match = 0 if (pref_c and pref_c in c) else 1
        size = file_size_gb(t.file_size) or 999
        return (q_exact, q_rank, c_match, size)

    pool = torrents
    if max_gb and max_gb > 0:
        capped = [t for t in torrents
                  if (file_size_gb(t.file_size) or 0) <= max_gb]
        pool = capped or torrents
    return sorted(pool, key=keyfn)[0]


# ── delivery ─────────────────────────────────────────────────────────────

def _safe_filename(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9 ._()\[\]-]", "_", name)[:150]


def send_movie(session: Session, movie: Movie, torrent: MovieTorrent) -> bool:
    """Add to Radarr (metadata/monitoring) + qBittorrent (the download)."""
    ok_radarr = False
    radarr_url = st.get(session, "radarr_url") or ""
    if radarr_url:
        radarr = RadarrClient(radarr_url, st.get(session, "radarr_api_key") or "")
        profile = st.get_int(session, "radarr_quality_profile_id") or 1
        root = st.get(session, "radarr_root_folder") or None
        added = None
        if movie.tmdb_id:
            added = radarr.add_by_tmdb(movie.tmdb_id, profile, root)
        elif movie.imdb_id:
            added = radarr.add_by_imdb(movie.imdb_id, profile, root)
        if added:
            ok_radarr = True
            movie.added_to_radarr = True
        else:
            movie.radarr_skip_reason = ("not_in_tmdb" if not movie.tmdb_id
                                        else "radarr_add_failed")
            log.warning(f"Radarr skipped for '{movie.title}': {movie.radarr_skip_reason}")

    qb_url = st.get(session, "qbittorrent_url") or ""
    ok_qb = False
    if qb_url:
        qb = QBittorrentClient(qb_url,
                               st.get(session, "qbittorrent_username") or "",
                               st.get(session, "qbittorrent_password") or "")
        category = st.get(session, "qbittorrent_category") or "radarr"
        if torrent.is_magnet:
            ok_qb = qb.add_torrent_url(torrent.torrent_url, category)
        else:
            path = torrent.torrent_file_path
            if not path or not os.path.exists(path):
                try:
                    path = str(env.torrents_dir / (_safe_filename(torrent.name) + ".torrent"))
                    download_file(torrent.torrent_url, path)
                    torrent.torrent_file_path = path
                except Exception as e:
                    log.warning(f"Torrent file download failed, trying by URL: {e}")
                    path = None
            ok_qb = (qb.add_torrent_file(path, category) if path
                     else qb.add_torrent_url(torrent.torrent_url, category))
        movie.added_to_qbittorrent = ok_qb

    movie.selected_torrent_id = torrent.id
    movie.downloaded_quality = torrent.quality

    if ok_qb:
        movie.status = MovieStatus.SENT
        movie.rejection_reason = None
        log.info(f"SENT: '{movie.title}' ({movie.year}) {torrent.quality} "
                 f"[radarr={ok_radarr}, qbittorrent={ok_qb}]")
        return True
    movie.status = MovieStatus.FAILED
    movie.rejection_reason = "qbittorrent_failed" if qb_url else "qbittorrent_not_configured"
    log.error(f"Send failed for '{movie.title}': {movie.rejection_reason}")
    return False


# ── full flows ───────────────────────────────────────────────────────────

def process_topic(session: Session, topic: dict, source: str = "auto_scan",
                  auto_download: bool = True) -> dict:
    """Auto-scan flow for one forum topic. Returns a summary dict."""
    parsed = parse_movie_title_year(topic["text"])
    title, year = parsed["title"], parsed["year"]

    movie, created = get_or_create_movie(session, title, year,
                                         topic["href"], topic["text"], source)
    if not created and movie.status in (MovieStatus.SENT,):
        return {"movie_id": movie.id, "title": title, "status": "duplicate"}

    try:
        torrents, post_text = fetch_post(movie)
        store_torrents(session, movie, torrents)
    except Exception as e:
        log.error(f"Post fetch failed for '{title}': {e}", exc=e)
        movie.status = MovieStatus.FAILED
        movie.rejection_reason = "post_fetch_failed"
        return {"movie_id": movie.id, "title": title, "status": "error"}

    engine = MatchEngine(session)
    result = engine.match(title, year, movie.forum_languages, post_text)
    apply_match(session, movie, result)

    if movie.status in (MovieStatus.NEEDS_REVIEW, MovieStatus.UNMATCHED):
        return {"movie_id": movie.id, "title": title, "status": movie.status}

    # Duplicate guard: Radarr already has this movie → never download twice
    rm = radarr_lookup(session, movie)
    if rm:
        movie.status = MovieStatus.IN_RADARR
        movie.added_to_radarr = True
        movie.rejection_reason = ("already in Radarr (downloaded)"
                                  if rm.get("hasFile")
                                  else "already in Radarr (monitored)")
        log.info(f"Skipped '{title}': {movie.rejection_reason}")
        return {"movie_id": movie.id, "title": title, "status": movie.status}

    threshold = st.get_float(session, "rating_threshold")
    if movie.rating is None:
        movie.status = MovieStatus.NEEDS_REVIEW
        movie.rejection_reason = "no_rating_found"
        return {"movie_id": movie.id, "title": title, "status": movie.status}
    if movie.rating < threshold:
        movie.status = MovieStatus.REJECTED
        movie.rejection_reason = f"rating {movie.rating} < threshold {threshold}"
        log.info(f"Rejected '{title}': {movie.rejection_reason}")
        return {"movie_id": movie.id, "title": title, "status": movie.status}

    movie.status = MovieStatus.QUALIFIED
    if not auto_download:
        return {"movie_id": movie.id, "title": title, "status": movie.status}

    torrent = select_torrent(session, movie.torrents)
    if not torrent:
        movie.status = MovieStatus.FAILED
        movie.rejection_reason = "no_torrents_in_post"
        return {"movie_id": movie.id, "title": title, "status": movie.status}

    send_movie(session, movie, torrent)
    return {"movie_id": movie.id, "title": title, "status": movie.status}


def catalog_topic(session: Session, topic: dict) -> dict:
    """Library-scan flow: metadata + torrents only, never downloads."""
    parsed = parse_movie_title_year(topic["text"])
    title, year = parsed["title"], parsed["year"]

    movie, created = get_or_create_movie(session, title, year,
                                         topic["href"], topic["text"],
                                         "library_scan")
    if not created:
        return {"movie_id": movie.id, "status": "skipped"}

    try:
        torrents, post_text = fetch_post(movie)
        store_torrents(session, movie, torrents)
    except Exception as e:
        log.warning(f"Library: post fetch failed for '{title}': {e}")
        post_text = None

    try:
        result = MatchEngine(session).match(title, year,
                                            movie.forum_languages, post_text)
        apply_match(session, movie, result)
    except Exception as e:
        log.warning(f"Library: metadata failed for '{title}': {e}")

    # library entries are never auto-sent; normalize status
    if movie.status in (MovieStatus.MATCHED, MovieStatus.DISCOVERED):
        movie.status = MovieStatus.LIBRARY
    return {"movie_id": movie.id, "status": "cataloged"}


def download_movie(session: Session, movie: Movie,
                   torrent_id: int | None = None) -> dict:
    """Manual 'download anyway' — from review queue, rejected list, library
    or search results. Bypasses the rating threshold."""
    torrent = None
    if torrent_id:
        torrent = next((t for t in movie.torrents if t.id == torrent_id), None)
    torrent = torrent or select_torrent(session, movie.torrents)
    if not torrent:
        return {"ok": False, "error": "no torrents stored for this movie"}
    ok = send_movie(session, movie, torrent)
    return {"ok": ok, "status": movie.status}


def apply_review_choice(session: Session, movie: Movie, candidate_idx: int) -> dict:
    """User picked one of the stored match candidates in the review UI."""
    cands = movie.match_candidates or []
    if not (0 <= candidate_idx < len(cands)):
        return {"ok": False, "error": "bad candidate index"}
    best = dict(cands[candidate_idx])
    best.setdefault("_score", best.get("_score", 1.0))
    enriched = MatchEngine(session)._enrich(best)
    movie.poster_path = None  # re-fetch poster for the chosen film
    apply_match(session, movie,
                {"status": "matched", "best": enriched, "candidates": cands})
    movie.match_confidence = 1.0  # human-confirmed
    log.info(f"Review: '{movie.title}' confirmed as "
             f"'{enriched.get('title')}' ({enriched.get('year')})")
    return {"ok": True}
