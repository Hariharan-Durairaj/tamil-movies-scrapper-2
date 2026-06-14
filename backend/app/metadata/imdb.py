"""IMDb without an API key.

- suggestion API (v3.sg.media-imdb.com / v2.sg... JSON): reliable search, no
  scraping. Covers small Tamil films that are missing from TMDB.
- title page JSON-LD: rating + poster + language evidence for one title.
"""
from __future__ import annotations

import json
import re

import requests

from .. import log
from ..scraper.http import HEADERS

SUGGEST_URLS = [
    "https://v3.sg.media-imdb.com/suggestion/x/{q}.json",
    "https://v2.sg.media-imdb.com/suggestion/{first}/{q}.json",
]


def _resize_poster(url: str | None, width: int = 342) -> str | None:
    """IMDb image URLs support inline resize params: ..._V1_UX342_.jpg."""
    if not url:
        return None
    return re.sub(r"\._V1_.*?\.jpg$", f"._V1_UX{width}_.jpg", url)


def suggest(title: str) -> list[dict]:
    """IMDb suggestion API → candidate dicts (movies only)."""
    q = re.sub(r"[^a-z0-9 ]", "", title.lower()).strip().replace(" ", "_")
    if not q:
        return []
    for tmpl in SUGGEST_URLS:
        url = tmpl.format(q=q, first=q[0])
        try:
            r = requests.get(url, headers=HEADERS, timeout=15)
            if r.status_code != 200:
                continue
            items = (r.json().get("d") or [])
            out = []
            for it in items:
                imdb_id = it.get("id") or ""
                if not imdb_id.startswith("tt"):
                    continue
                qid = (it.get("qid") or it.get("q") or "").lower()
                if qid and qid not in ("movie", "feature", "tvmovie", "tv movie", "video"):
                    continue
                out.append({
                    "source": "imdb",
                    "tmdb_id": None,
                    "imdb_id": imdb_id,
                    "title": it.get("l"),
                    "original_title": it.get("l"),
                    "year": it.get("y"),
                    "original_language": None,
                    "rating": None,                      # filled by title_data()
                    "votes": 0,
                    "poster_url": _resize_poster((it.get("i") or {}).get("imageUrl")),
                    "stars": (it.get("s") or "").lower(),  # "vijay, pooja hegde"
                })
            return out[:8]
        except Exception as e:
            log.debug(f"IMDb suggest failed ({url}): {e}")
    return []


def title_data(imdb_id: str) -> dict | None:
    """Fetch a title page and parse JSON-LD: rating, poster, genres, cast."""
    url = f"https://www.imdb.com/title/{imdb_id}/"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20)
        if r.status_code != 200:
            log.debug(f"IMDb title page HTTP {r.status_code} for {imdb_id}")
            return None
        m = re.search(
            r'<script type="application/ld\+json">(.*?)</script>',
            r.text, re.DOTALL)
        if not m:
            return None
        data = json.loads(m.group(1))
        agg = data.get("aggregateRating") or {}
        actors = data.get("actor") or []
        if isinstance(actors, dict):
            actors = [actors]
        return {
            "imdb_id": imdb_id,
            "title": data.get("name"),
            "rating": float(agg["ratingValue"]) if agg.get("ratingValue") else None,
            "votes": int(agg.get("ratingCount") or 0),
            "poster_url": _resize_poster(data.get("image")),
            "actors": [a.get("name", "").lower() for a in actors],
        }
    except Exception as e:
        log.warning(f"IMDb title page parse failed for {imdb_id}: {e}")
        return None
