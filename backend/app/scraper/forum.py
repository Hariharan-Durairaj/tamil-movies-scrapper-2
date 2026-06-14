"""Forum listing / search / pagination scraping (IPS Invision Community)."""
from __future__ import annotations

import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from .. import log
from .http import fetch_soup


def page_url(forum_url: str, page: int) -> str:
    """Page 1: the forum URL itself; page N: .../page/N/."""
    if page <= 1:
        return forum_url
    return forum_url.rstrip("/") + f"/page/{page}/"


def list_topics(url: str) -> list[dict]:
    """Topic links on a forum listing page (data-ipshover anchors).
    Returns [{'href', 'text', 'title'}], newest first (forum order)."""
    soup = fetch_soup(url)
    links, seen = [], set()
    for link in soup.find_all("a", attrs={"data-ipshover": True}):
        href = link.get("href")
        if not href:
            continue
        if not href.startswith("http"):
            href = urljoin(url, href)
        if "/topic/" not in href:
            continue
        # strip per-comment anchors so dedupe works
        href = href.split("#")[0].split("&comment=")[0]
        if href in seen:
            continue
        span = link.find("span")
        text = (span or link).get_text(strip=True)
        if not text:
            continue
        seen.add(href)
        links.append({"href": href, "text": text, "title": link.get("title") or ""})
    log.debug(f"Forum page: {len(links)} topics", url=url)
    return links


def search_results(url: str) -> list[dict]:
    """Search result links: <a data-linktype="link" href="...">Title</a>."""
    soup = fetch_soup(url)
    out = []
    for link in soup.find_all("a", attrs={"data-linktype": "link"}):
        href = link.get("href")
        text = link.get_text(strip=True)
        if href and not href.startswith("http"):
            href = urljoin(url, href)
        if href and text:
            out.append({"href": href.split("#")[0], "text": text})
    log.debug(f"Search: {len(out)} results", url=url)
    return out


def total_pages(url: str) -> int:
    """Read total page count from the IPS pagination block; 1 if absent."""
    try:
        soup = fetch_soup(url)
    except Exception as e:
        log.warning(f"total_pages fetch failed: {e}", url=url)
        return 1
    pagination = soup.find("ul", class_="ipsPagination")
    if not pagination:
        return 1
    for attr in ("data-pages", "data-ipspagination-pages"):
        val = pagination.get(attr)
        if val and str(val).strip().isdigit():
            return int(str(val).strip())
    page_input = pagination.find("input", attrs={"max": True})
    if page_input and str(page_input.get("max", "")).strip().isdigit():
        return int(str(page_input["max"]).strip())
    m = re.search(r"Page\s+\d+\s+of\s+(\d+)",
                  pagination.get_text(" ", strip=True), re.IGNORECASE)
    return int(m.group(1)) if m else 1
