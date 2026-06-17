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
from ..scraper.parse import (detect_languages, file_size_gb,
                             normalize_title, parse_movie_title_year)
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

    # Fuzzy fallback: same year, title normalizes identically (e.g. forum
    # "KD: The Devil" vs Radarr "KD – The Devil"). Avoids punctuation dupes.
    norm = normalize_title(title)
    if norm:
        yq = session.query(Movie)
        yq = yq.filter(Movie.year == year) if year is not None else yq.filter(Movie.year.is_(None))
        for cand in yq.all():
            if normalize_title(cand.title) == norm or (
                    cand.matched_title and normalize_title(cand.matched_title) == norm):
                return cand, False
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
        # Append to the relationship (not just session.add with movie_id) so the
        # in-memory movie.torrents collection is up to date. Otherwise a freshly
        # created movie keeps a stale empty collection and select_torrent() later
        # in the same transaction sees no torrents → false "no_torrents_in_post".
        movie.torrents.append(MovieTorrent(
            name=t["name"], torrent_url=t["torrent_url"],
            is_magnet=t["is_magnet"], source_format=t["source_format"],
            quality=t["quality"], codec=t["codec"], rip_type=t["rip_type"],
            file_size=t["file_size"], languages=t["languages"]))
        existing.add(t["torrent_url"])
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


def merge_external_duplicates(session: Session, movie: Movie) -> Movie:
    """Collapse rows that point at the same film (same tmdb_id or imdb_id).

    Radarr-sync rows (Radarr's title) and auto-scan rows (forum-parsed title)
    used to coexist because their titles differ. Now that the movie has an
    external id we can merge them. The canonical row is the one that has forum
    data (so we keep the post/torrents); the other's useful fields are folded
    in and it is deleted. Returns the surviving row."""
    if not (movie.tmdb_id or movie.imdb_id):
        return movie

    conds = []
    if movie.tmdb_id:
        conds.append(Movie.tmdb_id == movie.tmdb_id)
    if movie.imdb_id:
        conds.append(func.lower(Movie.imdb_id) == movie.imdb_id.lower())
    from sqlalchemy import or_ as _or
    dupes = (session.query(Movie)
             .filter(_or(*conds), Movie.id != movie.id).all())
    if not dupes:
        return movie

    canonical = movie
    for other in dupes:
        # Prefer the row that has the forum post as canonical.
        keep, drop = (canonical, other)
        if other.forum_url and not canonical.forum_url:
            keep, drop = other, canonical

        # Fold useful fields from drop into keep where missing.
        for attr in ("forum_url", "forum_title", "forum_languages",
                     "imdb_id", "tmdb_id", "rating", "rating_source",
                     "matched_title", "original_language", "poster_path",
                     "match_confidence", "match_candidates"):
            if getattr(keep, attr) in (None, "", []) and getattr(drop, attr) not in (None, "", []):
                setattr(keep, attr, getattr(drop, attr))
        if keep.is_tamil_original is None and drop.is_tamil_original is not None:
            keep.is_tamil_original = drop.is_tamil_original
        keep.added_to_radarr = keep.added_to_radarr or drop.added_to_radarr
        keep.added_to_qbittorrent = keep.added_to_qbittorrent or drop.added_to_qbittorrent
        # A real delivery / radarr status wins over a bare "matched".
        rank = {MovieStatus.SENT: 5, MovieStatus.IN_RADARR: 4,
                MovieStatus.QUALIFIED: 3, MovieStatus.MATCHED: 2}
        if rank.get(drop.status, 0) > rank.get(keep.status, 0):
            keep.status = drop.status
            keep.rejection_reason = drop.rejection_reason

        # Move torrents that keep doesn't already have.
        have = {t.torrent_url for t in keep.torrents}
        for t in list(drop.torrents):
            if t.torrent_url not in have:
                t.movie_id = keep.id
                have.add(t.torrent_url)
        session.flush()
        session.delete(drop)
        session.flush()
        canonical = keep
        log.info(f"Merged duplicate '{drop.title}' into '{keep.title}' "
                 f"(tmdb={keep.tmdb_id}, imdb={keep.imdb_id})")
    return canonical


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


def _is_torrent_file(path: str) -> bool:
    """A real .torrent is a bencoded dict — its first byte is 'd'. Forum
    attachment links sometimes return an HTML login/error page instead, which
    qBittorrent rejects (e.g. HTTP 409). Detect that so we can fall back."""
    try:
        with open(path, "rb") as f:
            return f.read(1) == b"d"
    except Exception:
        return False


def send_movie(session: Session, movie: Movie, torrent: MovieTorrent) -> bool:
    """Add to Radarr (metadata/monitoring) + qBittorrent (the download)."""
    ok_radarr = False
    radarr_url = st.get(session, "radarr_url") or ""
    if radarr_url:
        # If the film is already in Radarr (e.g. monitored but missing a file)
        # don't re-add it — qBittorrent will deliver the download and Radarr
        # imports it. Otherwise add it now.
        if radarr_lookup(session, movie):
            ok_radarr = True
            movie.added_to_radarr = True
        else:
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
            # Skip an invalid file (HTML login/error page) — it would be rejected.
            if path and not _is_torrent_file(path):
                log.warning(f"'{movie.title}': downloaded file is not a valid "
                            f".torrent (looks like HTML) — falling back to URL")
                path = None
            ok_qb = qb.add_torrent_file(path, category) if path else False
            if not ok_qb:
                # File upload failed/rejected (e.g. HTTP 409 behind a reverse
                # proxy) or no usable file — hand qBittorrent the URL instead.
                log.info(f"'{movie.title}': retrying qBittorrent add via URL")
                ok_qb = qb.add_torrent_url(torrent.torrent_url, category)
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

    # Collapse any pre-existing row for the same film (e.g. a Radarr-sync row).
    movie = merge_external_duplicates(session, movie)

    if movie.status in (MovieStatus.NEEDS_REVIEW, MovieStatus.UNMATCHED):
        return {"movie_id": movie.id, "title": title, "status": movie.status}

    # Radarr guard. If Radarr already HAS the file, skip — never download twice.
    # If it's in Radarr but MISSING a file, it was already approved into the
    # library, so grab the torrent and send to qBittorrent only (send_movie
    # detects the existing Radarr entry and won't re-add it). Rating / dub gates
    # are bypassed here because the film is already a wanted Radarr entry.
    rm = radarr_lookup(session, movie)
    if rm:
        movie.added_to_radarr = True
        if rm.get("hasFile"):
            movie.status = MovieStatus.IN_RADARR
            movie.rejection_reason = "already in Radarr (downloaded)"
            log.info(f"Skipped '{title}': {movie.rejection_reason}")
            return {"movie_id": movie.id, "title": title, "status": movie.status}
        # in Radarr, no file yet → fetch it via qBittorrent
        if not auto_download:
            movie.status = MovieStatus.QUALIFIED
            return {"movie_id": movie.id, "title": title, "status": movie.status}
        torrent = select_torrent(session, movie.torrents)
        if not torrent:
            movie.status = MovieStatus.IN_RADARR
            movie.rejection_reason = "in Radarr (no file) — no torrent in post"
            return {"movie_id": movie.id, "title": title, "status": movie.status}
        log.info(f"'{title}' in Radarr without a file — sending torrent to qBittorrent")
        send_movie(session, movie, torrent)
        return {"movie_id": movie.id, "title": title, "status": movie.status}

    # Tamil-dub gate: when dubs are disabled, a non-Tamil-original (an Indian
    # film with a Tamil dub) must not be sent to Radarr.
    if not st.get_bool(session, "allow_tamil_dubs") and movie.is_tamil_original is False:
        movie.status = MovieStatus.REJECTED
        movie.rejection_reason = "Tamil dub (non-Tamil original) — dubs disabled"
        log.info(f"Rejected '{title}': {movie.rejection_reason}")
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


def set_imdb_id(session: Session, movie: Movie, imdb_id: str) -> dict:
    """Pin a movie to an IMDb id (manual library edit), then enrich it:
    resolve TMDB id via /find, pull rating/poster/language, mark confirmed."""
    engine = MatchEngine(session)
    cand = {"source": "manual", "imdb_id": imdb_id, "tmdb_id": None,
            "title": movie.matched_title or movie.title, "year": movie.year}
    if engine.tmdb:
        found = engine.tmdb.find_by_imdb(imdb_id)
        if found:
            cand.update({k: v for k, v in found.items() if v is not None})
    cand["imdb_id"] = imdb_id
    cand["_score"] = 1.0
    enriched = engine._enrich(cand)
    movie.poster_path = None  # force fresh poster for the corrected film
    apply_match(session, movie,
                {"status": "matched", "best": enriched,
                 "candidates": movie.match_candidates or []})
    movie.match_confidence = 1.0  # human-confirmed
    movie = merge_external_duplicates(session, movie)
    log.info(f"IMDb id set for movie {movie.id}: {imdb_id} "
             f"→ {enriched.get('title')} ({enriched.get('original_language')})")
    return movie_summary(movie)


def movie_summary(movie: Movie) -> dict:
    return {"ok": True, "id": movie.id, "imdb_id": movie.imdb_id,
            "tmdb_id": movie.tmdb_id, "rating": movie.rating,
            "matched_title": movie.matched_title,
            "original_language": movie.original_language,
            "is_tamil_original": movie.is_tamil_original,
            "status": movie.status,
            "poster": f"/posters/{movie.poster_path}" if movie.poster_path else None}


def reset_all_data() -> dict:
    """Delete every data row but keep the settings table. Also wipes cached
    poster/torrent files on disk."""
    from ..db.models import (DomainHistory, LogEntry, MetadataCache,
                             TaskState)
    counts = {}
    with session_scope() as session:
        for model, name in ((MovieTorrent, "torrents"), (Movie, "movies"),
                            (LogEntry, "logs"), (MetadataCache, "metadata_cache"),
                            (TaskState, "task_state"), (DomainHistory, "domain_history")):
            counts[name] = session.query(model).delete(synchronize_session=False)
        # full-scan checkpoint lives in the settings table — reset it too
        st.set_value(session, "full_scan_last_page", "0")
        session.commit()
        session.expire_all()

    # wipe cached files
    removed_files = 0
    for d in (env.posters_dir, env.torrents_dir):
        try:
            for f in d.glob("*"):
                if f.is_file():
                    f.unlink()
                    removed_files += 1
        except Exception as e:
            log.warning(f"reset_all: could not clean {d}: {e}")

    invalidate_radarr_cache()
    log.info(f"Reset all data: {counts}, {removed_files} files removed")
    return {"ok": True, "deleted": counts, "files_removed": removed_files}


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
