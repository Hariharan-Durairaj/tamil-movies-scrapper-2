"""OMDB client — supplementary source: IMDb rating + language string."""
from __future__ import annotations

import requests

from .. import log

BASE = "https://www.omdbapi.com/"


class OMDBClient:
    def __init__(self, api_key: str):
        self.api_key = api_key

    def _get(self, **params) -> dict | None:
        params["apikey"] = self.api_key
        try:
            r = requests.get(BASE, params=params, timeout=15)
            data = r.json()
            if data.get("Response") == "True":
                return data
        except Exception as e:
            log.warning(f"OMDB lookup failed: {e}")
        return None

    def by_title(self, title: str, year: int | None = None) -> dict | None:
        params = {"t": title, "type": "movie"}
        if year:
            params["y"] = year
        data = self._get(**params)
        return self._to_candidate(data) if data else None

    def by_imdb_id(self, imdb_id: str) -> dict | None:
        data = self._get(i=imdb_id)
        return self._to_candidate(data) if data else None

    @staticmethod
    def _to_candidate(d: dict) -> dict:
        rating = d.get("imdbRating")
        year = d.get("Year") or ""
        return {
            "source": "omdb",
            "tmdb_id": None,
            "imdb_id": d.get("imdbID"),
            "title": d.get("Title"),
            "original_title": d.get("Title"),
            "year": int(year[:4]) if year[:4].isdigit() else None,
            "original_language": None,          # OMDB gives names, not codes
            "language_names": (d.get("Language") or "").lower(),  # "tamil, telugu"
            "rating": float(rating) if rating and rating != "N/A" else None,
            "votes": 0,
            "poster_url": d.get("Poster") if d.get("Poster") != "N/A" else None,
        }
