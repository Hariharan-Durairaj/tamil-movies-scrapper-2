"""Metadata matching engine — the fix for the 'Beast' problem.

Instead of taking the first acceptable hit from a source chain, we:
  1. gather candidates from TMDB (with & without year) AND the IMDb
     suggestion API (covers small Tamil films missing from TMDB),
  2. score every candidate on title similarity, year, language and
     forum-post evidence (advertised audio languages, cast names),
  3. auto-accept only above a confidence threshold; ambiguous matches are
     stored as `needs_review` with the top candidates for one-click pick.

All external lookups are cached in the metadata_cache table.
"""
from __future__ import annotations

import math

from sqlalchemy.orm import Session

from .. import log
from ..db import settings_store as st
from ..db.models import MetadataCache
from ..scraper.parse import title_similarity
from . import imdb
from .omdb import OMDBClient
from .tmdb import TMDBClient

INDIAN_LANGS = {"ta", "te", "ml", "kn", "hi", "bn", "mr", "pa", "gu"}
LANG_NAME_TO_CODE = {"tamil": "ta", "telugu": "te", "malayalam": "ml",
                     "kannada": "kn", "hindi": "hi", "english": "en"}

W_TITLE, W_YEAR, W_LANG, W_POP = 0.45, 0.15, 0.30, 0.10


# ── cache ────────────────────────────────────────────────────────────────

def _cached(session: Session, source: str, key: str, fn):
    row = (session.query(MetadataCache)
           .filter_by(source=source, query_key=key).one_or_none())
    if row is not None:
        return row.payload
    payload = fn()
    session.add(MetadataCache(source=source, query_key=key, payload=payload))
    session.flush()
    return payload


# ── scoring ──────────────────────────────────────────────────────────────

def _year_score(forum_year: int | None, cand_year: int | None) -> float:
    if forum_year is None or cand_year is None:
        return 0.3
    diff = abs(forum_year - cand_year)
    return 1.0 if diff == 0 else 0.7 if diff == 1 else 0.0


def _lang_score(cand: dict, forum_languages: list[str]) -> float:
    """ta=1.0, other Indian=0.55, unknown=0.4, Western=0.0 (0.2 if the post
    itself looks like a non-Indian release, e.g. 'english' listed first)."""
    code = (cand.get("original_language") or "").lower()
    names = cand.get("language_names") or ""

    if not code and names:                    # OMDB language names string
        if "tamil" in names:
            return 1.0
        if any(l in names for l in ("telugu", "malayalam", "kannada", "hindi")):
            return 0.55

    if code == "ta":
        return 1.0
    if code in INDIAN_LANGS:
        return 0.55
    if not code:
        return 0.4
    # Western/other language: almost always the wrong film for this forum —
    # unless the post itself advertises that language first (rare).
    if forum_languages and forum_languages[0] == "english":
        return 0.2
    return 0.0


def _pop_score(votes: int) -> float:
    """log-scaled vote count, saturates ~100k votes. Tiebreaker only."""
    if not votes:
        return 0.0
    return min(1.0, math.log10(votes + 1) / 5.0)


def score_candidate(cand: dict, title: str, year: int | None,
                    forum_languages: list[str]) -> float:
    t1 = title_similarity(title, cand.get("title") or "")
    t2 = title_similarity(title, cand.get("original_title") or "")
    s = (W_TITLE * max(t1, t2)
         + W_YEAR * _year_score(year, cand.get("year"))
         + W_LANG * _lang_score(cand, forum_languages)
         + W_POP * _pop_score(cand.get("votes") or 0))
    return round(s, 4)


# ── matching ─────────────────────────────────────────────────────────────

class MatchEngine:
    def __init__(self, session: Session):
        self.session = session
        tmdb_key = st.get(session, "tmdb_api_key") or ""
        omdb_key = st.get(session, "omdb_api_key") or ""
        self.tmdb = TMDBClient(tmdb_key) if tmdb_key else None
        self.omdb = OMDBClient(omdb_key) if omdb_key else None

    # -- candidate gathering ------------------------------------------------
    def _gather(self, title: str, year: int | None) -> list[dict]:
        cands: list[dict] = []

        if self.tmdb:
            key = f"{title.lower()}|{year}"
            cands += _cached(self.session, "tmdb_search", key,
                             lambda: self.tmdb.search(title, year)) or []
            if year:  # forum year is sometimes wrong — search without it too
                cands += _cached(self.session, "tmdb_search", f"{title.lower()}|None",
                                 lambda: self.tmdb.search(title)) or []

        cands += _cached(self.session, "imdb_suggest", title.lower(),
                         lambda: imdb.suggest(title)) or []

        if self.omdb:
            key = f"{title.lower()}|{year}"
            c = _cached(self.session, "omdb_title", key,
                        lambda: self.omdb.by_title(title, year))
            if c:
                cands.append(c)

        # dedupe: same tmdb_id or same imdb_id
        seen, out = set(), []
        for c in cands:
            k = ("tmdb", c.get("tmdb_id")) if c.get("tmdb_id") else \
                ("imdb", c.get("imdb_id")) if c.get("imdb_id") else \
                ("t", (c.get("title"), c.get("year")))
            if k in seen:
                continue
            seen.add(k)
            out.append(c)
        return out

    # -- evidence boost for close calls --------------------------------------
    def _evidence_boost(self, cands: list[dict], post_text: str | None) -> None:
        """If the forum post text names cast members, boost candidates whose
        cast appears in it. Only called when the decision is ambiguous."""
        if not post_text:
            return
        text = post_text.lower()
        for c in cands[:4]:
            names: list[str] = []
            if c.get("stars"):
                names = [n.strip() for n in c["stars"].split(",") if n.strip()]
            elif c.get("tmdb_id") and self.tmdb:
                names = _cached(self.session, "tmdb_credits", str(c["tmdb_id"]),
                                lambda tid=c["tmdb_id"]: self.tmdb.credits(tid)) or []
            hits = sum(1 for n in names if n and n in text)
            if hits:
                c["_score"] = round(min(1.0, c["_score"] + 0.08 * min(hits, 3)), 4)
                c["_evidence"] = f"{hits} cast name(s) found in post"

    # -- enrichment of the chosen candidate ----------------------------------
    def _enrich(self, cand: dict) -> dict:
        """Make sure the winner has imdb_id, a real rating and a poster."""
        if cand.get("tmdb_id") and not cand.get("imdb_id") and self.tmdb:
            ids = _cached(self.session, "tmdb_external", str(cand["tmdb_id"]),
                          lambda: self.tmdb.external_ids(cand["tmdb_id"]))
            cand["imdb_id"] = (ids or {}).get("imdb_id")

        # Prefer a true IMDb rating over TMDB vote_average
        if cand.get("imdb_id"):
            data = _cached(self.session, "imdb_title", cand["imdb_id"],
                           lambda: imdb.title_data(cand["imdb_id"]))
            if data:
                if data.get("rating"):
                    cand["rating"] = data["rating"]
                    cand["rating_source"] = "imdb"
                cand["poster_url"] = cand.get("poster_url") or data.get("poster_url")
            if cand.get("rating") is None and self.omdb:
                o = _cached(self.session, "omdb_id", cand["imdb_id"],
                            lambda: self.omdb.by_imdb_id(cand["imdb_id"]))
                if o and o.get("rating"):
                    cand["rating"] = o["rating"]
                    cand["rating_source"] = "omdb"
        if cand.get("rating") and not cand.get("rating_source"):
            cand["rating_source"] = cand["source"]
        return cand

    # -- public ---------------------------------------------------------------
    def match(self, title: str, year: int | None,
              forum_languages: list[str] | None = None,
              post_text: str | None = None) -> dict:
        """Returns {'status', 'best', 'candidates'} where status is
        matched | needs_review | unmatched."""
        forum_languages = forum_languages or []
        auto_accept = st.get_float(self.session, "match_auto_accept") or 0.75
        review_floor = st.get_float(self.session, "match_review_floor") or 0.40

        cands = self._gather(title, year)
        for c in cands:
            c["_score"] = score_candidate(c, title, year, forum_languages)
        cands.sort(key=lambda c: c["_score"], reverse=True)

        if not cands:
            log.info(f"Metadata: no candidates for '{title}' ({year})")
            return {"status": "unmatched", "best": None, "candidates": []}

        # Ambiguous? bring in cast evidence, then re-rank.
        if cands[0]["_score"] < auto_accept or (
                len(cands) > 1 and cands[0]["_score"] - cands[1]["_score"] < 0.08):
            self._evidence_boost(cands, post_text)
            cands.sort(key=lambda c: c["_score"], reverse=True)

        best = cands[0]
        if best["_score"] >= auto_accept:
            status = "matched"
        elif best["_score"] >= review_floor:
            status = "needs_review"
        else:
            status = "unmatched"

        if status != "unmatched":
            best = self._enrich(best)

        log.info(f"Metadata: '{title}' ({year}) → {status} "
                 f"[{best.get('title')} {best.get('year')}, "
                 f"lang={best.get('original_language')}, score={best['_score']}]")
        return {"status": status, "best": best, "candidates": cands[:5]}
