"""TMDB client — returns FULL candidate lists, not just the first hit."""
from __future__ import annotations

import requests

from .. import log

BASE = "https://api.themoviedb.org/3"
POSTER_BASE = "https://image.tmdb.org/t/p/w342"   # small size — fast download


class TMDBClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, path: str, **params) -> dict | None:
        params["api_key"] = self.api_key
        try:
            r = requests.get(f"{BASE}{path}", params=params, timeout=15)
            if r.status_code == 200:
                return r.json()
            log.warning(f"TMDB {path} HTTP {r.status_code}")
        except Exception as e:
            log.warning(f"TMDB {path} failed: {e}")
        return None

    def search(self, title: str, year: int | None = None) -> list[dict]:
        """Search results as candidate dicts."""
        params = {"query": title}
        if year:
            params["primary_release_year"] = year
        data = self._get("/search/movie", **params)
        results = (data or {}).get("results") or []
        return [self._to_candidate(r) for r in results[:10]]

    def external_ids(self, tmdb_id: int) -> dict:
        return self._get(f"/movie/{tmdb_id}/external_ids") or {}

    def details(self, tmdb_id: int) -> dict | None:
        return self._get(f"/movie/{tmdb_id}")

    def credits(self, tmdb_id: int) -> list[str]:
        """Top cast + director names, lowercased (used as match evidence)."""
        data = self._get(f"/movie/{tmdb_id}/credits") or {}
        names = [c.get("name", "") for c in (data.get("cast") or [])[:10]]
        names += [c.get("name", "") for c in (data.get("crew") or [])
                  if c.get("job") == "Director"]
        return [n.lower() for n in names if n]

    @staticmethod
    def _to_candidate(r: dict) -> dict:
        release = r.get("release_date") or ""
        return {
            "source": "tmdb",
            "tmdb_id": r.get("id"),
            "imdb_id": None,                       # filled later if chosen
            "title": r.get("title"),
            "original_title": r.get("original_title"),
            "year": int(release[:4]) if release[:4].isdigit() else None,
            "original_language": r.get("original_language"),
            "rating": r.get("vote_average") or None,
            "votes": r.get("vote_count") or 0,
            "poster_url": (POSTER_BASE + r["poster_path"]) if r.get("poster_path") else None,
        }
