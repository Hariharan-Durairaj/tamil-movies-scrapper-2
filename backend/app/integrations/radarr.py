"""Radarr v3 API client."""
from __future__ import annotations

import requests

from .. import log


class RadarrClient:
    def __init__(self, url: str, api_key: str):
        self.url = url.rstrip("/")
        self.headers = {"X-Api-Key": api_key}

    def _get(self, path: str, **params):
        r = requests.get(f"{self.url}/api/v3{path}", headers=self.headers,
                         params=params, timeout=15)
        r.raise_for_status()
        return r.json()

    def test_connection(self) -> bool:
        try:
            requests.get(f"{self.url}/api/v3/system/status",
                         headers=self.headers, timeout=10).raise_for_status()
            return True
        except Exception as e:
            log.warning(f"Radarr connection test failed: {e}")
            return False

    def get_movies(self) -> list[dict]:
        try:
            return self._get("/movie")
        except Exception as e:
            log.warning(f"Radarr get_movies failed: {e}")
            return []

    def find_by_tmdb(self, tmdb_id: int) -> dict | None:
        for m in self.get_movies():
            if m.get("tmdbId") == tmdb_id:
                return m
        return None

    def root_folder(self) -> str | None:
        try:
            folders = self._get("/rootfolder")
            return folders[0]["path"] if folders else None
        except Exception as e:
            log.warning(f"Radarr rootfolder failed: {e}")
            return None

    def quality_profiles(self) -> list[dict]:
        try:
            return self._get("/qualityprofile")
        except Exception:
            return []

    def _add(self, lookup: dict, profile_id: int, root: str | None,
             search: bool = False) -> dict | None:
        """POST a lookup result. search=False because the torrent is delivered
        by us through qBittorrent — Radarr only needs to monitor/import."""
        root = root or self.root_folder()
        if not root:
            log.error("Radarr: no root folder configured")
            return None
        body = {
            "title": lookup.get("title"),
            "tmdbId": lookup.get("tmdbId"),
            "qualityProfileId": profile_id,
            "rootFolderPath": root,
            "monitored": True,
            "addOptions": {"searchForMovie": search},
        }
        r = requests.post(f"{self.url}/api/v3/movie", headers=self.headers,
                          json=body, timeout=15)
        if r.status_code in (200, 201):
            log.info(f"Radarr: added '{lookup.get('title')}'")
            return r.json()
        if r.status_code == 400 and "already" in r.text.lower():
            log.info(f"Radarr: '{lookup.get('title')}' already exists")
            return self.find_by_tmdb(lookup.get("tmdbId"))
        log.error(f"Radarr add failed: HTTP {r.status_code}", body=r.text[:500])
        return None

    def add_by_tmdb(self, tmdb_id: int, profile_id: int = 1,
                    root: str | None = None) -> dict | None:
        try:
            lookup = self._get("/movie/lookup/tmdb", tmdbId=tmdb_id)
            return self._add(lookup, profile_id, root)
        except Exception as e:
            log.error(f"Radarr add_by_tmdb({tmdb_id}) failed: {e}", exc=e)
            return None

    def add_by_imdb(self, imdb_id: str, profile_id: int = 1,
                    root: str | None = None) -> dict | None:
        """Works for some titles not in Radarr's TMDB-based index; returns
        None when Radarr can't resolve the IMDb id."""
        try:
            lookup = self._get("/movie/lookup/imdb", imdbId=imdb_id)
            if not lookup or not lookup.get("tmdbId"):
                return None
            return self._add(lookup, profile_id, root)
        except Exception as e:
            log.warning(f"Radarr add_by_imdb({imdb_id}) failed: {e}")
            return None
