"""qBittorrent WebUI API client."""
from __future__ import annotations

import requests

from .. import log


class QBittorrentClient:
    def __init__(self, url: str, username: str, password: str):
        self.url = url.rstrip("/")
        self.username = username
        self.password = password
        self.session = requests.Session()
        self.logged_in = False

    def login(self) -> bool:
        try:
            r = self.session.post(f"{self.url}/api/v2/auth/login",
                                  data={"username": self.username,
                                        "password": self.password}, timeout=10)
            # 204 = qBittorrent has "Bypass authentication for clients on localhost"
            # enabled, so the session is already authorized by IP (empty body, no
            # username/password check). Treat it as a successful login.
            self.logged_in = (
                r.status_code == 204
                or (r.status_code == 200 and r.text.strip().lower() == "ok.")
            )
            if not self.logged_in:
                log.warning(f"qBittorrent login failed: HTTP {r.status_code} {r.text[:100]}")
            return self.logged_in
        except Exception as e:
            log.warning(f"qBittorrent login error: {e}")
            return False

    def _ensure_login(self) -> bool:
        return self.logged_in or self.login()

    def test_connection(self) -> bool:
        return self.login()

    @staticmethod
    def _add_ok(r) -> bool:
        """Decide whether an /torrents/add call succeeded.

        Old qBittorrent replies with an empty body or 'Ok.'. qBittorrent v5
        replies with JSON like
        {"added_torrent_ids":[...],"failure_count":0,"success_count":1}.
        The old check `"fail" not in text` wrongly matched the *substring*
        'failure_count' and reported success as failure."""
        if r.status_code != 200:
            return False
        text = (r.text or "").strip()
        if not text or text.lower() == "ok.":
            return True
        try:
            data = r.json()
            if isinstance(data, dict) and (
                    "success_count" in data or "failure_count" in data
                    or "added_torrent_ids" in data):
                return (data.get("success_count", 0) > 0
                        or data.get("pending_count", 0) > 0
                        or bool(data.get("added_torrent_ids")))
        except Exception:
            pass
        # plain-text response that isn't JSON counts → treat literal 'fail' as error
        return "fail" not in text.lower()

    def add_torrent_url(self, url: str, category: str = "radarr") -> bool:
        """Add by URL — works for both magnet links and .torrent URLs."""
        if not self._ensure_login():
            return False
        try:
            r = self.session.post(f"{self.url}/api/v2/torrents/add",
                                  data={"urls": url, "category": category},
                                  timeout=30)
            ok = self._add_ok(r)
            if ok:
                log.info("qBittorrent: torrent added (url)", category=category)
            else:
                log.error(f"qBittorrent add url failed: HTTP {r.status_code} "
                          f"{r.reason} {r.text[:200]}")
            return ok
        except Exception as e:
            log.error(f"qBittorrent add url error: {e}", exc=e)
            return False

    def add_torrent_file(self, path: str, category: str = "radarr") -> bool:
        if not self._ensure_login():
            return False
        try:
            with open(path, "rb") as f:
                r = self.session.post(f"{self.url}/api/v2/torrents/add",
                                      files={"torrents": f},
                                      data={"category": category}, timeout=30)
            ok = self._add_ok(r)
            if ok:
                log.info("qBittorrent: torrent added (file)", path=path)
            else:
                log.error(f"qBittorrent add file failed: HTTP {r.status_code} "
                          f"{r.reason} {r.text[:200]}")
            return ok
        except Exception as e:
            log.error(f"qBittorrent add file error: {e}", exc=e)
            return False

    def list_torrents(self, category: str | None = None) -> list[dict]:
        if not self._ensure_login():
            return []
        try:
            params = {"category": category} if category else {}
            r = self.session.get(f"{self.url}/api/v2/torrents/info",
                                 params=params, timeout=15)
            return r.json() if r.status_code == 200 else []
        except Exception as e:
            log.warning(f"qBittorrent list error: {e}")
            return []
