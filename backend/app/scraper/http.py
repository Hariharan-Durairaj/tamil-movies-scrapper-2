"""One shared HTTP session for all forum scraping: browser UA, retries with
backoff, helpers for soup fetching and file downloads."""
from __future__ import annotations

import requests
from bs4 import BeautifulSoup
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

HEADERS = {
    "User-Agent": USER_AGENT,
    "Accept-Language": "en-US,en;q=0.9",
}


def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(HEADERS)
    retry = Retry(
        total=3, backoff_factor=1.5,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
    )
    adapter = HTTPAdapter(max_retries=retry)
    s.mount("http://", adapter)
    s.mount("https://", adapter)
    return s


_session = make_session()


def fetch(url: str, timeout: int = 20) -> requests.Response:
    resp = _session.get(url, timeout=timeout)
    resp.raise_for_status()
    return resp


def fetch_soup(url: str, timeout: int = 20) -> BeautifulSoup:
    return BeautifulSoup(fetch(url, timeout).content, "html.parser")


def download_file(url: str, dest_path: str, timeout: int = 60) -> str:
    resp = _session.get(url, timeout=timeout)
    resp.raise_for_status()
    with open(dest_path, "wb") as f:
        f.write(resp.content)
    return dest_path
