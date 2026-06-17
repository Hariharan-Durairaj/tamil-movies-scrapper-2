"""Torrent extraction from a forum post page.

Three formats exist on the forum (see docs/FORUM_HTML_FORMATS.md of v1);
the chain is: magnet → data-fileext → ipsAttachLink, first hit wins.
Magnets are preferred because they go straight to qBittorrent by URL and
avoid the signed attachment.php download (no host/key/referer to break).
All functions take a BeautifulSoup so they're testable on saved HTML.
"""
from __future__ import annotations

import re
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup

from .. import log
from .parse import clean_magnet_name, detect_languages, parse_torrent_name


def _mk(name: str, url: str, fmt: str, *, is_magnet=False, file_id=None) -> dict:
    return {
        "name": name,
        "torrent_url": url,
        "is_magnet": is_magnet,
        "file_id": file_id,
        "source_format": fmt,
        "languages": detect_languages(name),
        **parse_torrent_name(name),
    }


def parse_fileext(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Format 1 (current): <a data-fileext="torrent" href="...attachment.php?...">."""
    out = []
    for link in soup.find_all("a", attrs={"data-fileext": "torrent"}):
        href = link.get("href")
        if not href:
            continue
        span = link.find("span")
        name = None
        if span:
            strong = span.find("strong")
            name = (strong or span).get_text(strip=True)
        name = name or link.get_text(strip=True)
        if not name:
            continue
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        out.append(_mk(name, href.replace("&amp;", "&"), "fileext",
                       file_id=link.get("data-fileid")))
    return out


def parse_magnets(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Format 2: magnet links; release name comes from the dn parameter."""
    out, seen = [], set()
    for link in soup.select('a[href^="magnet:"]'):
        magnet = (link.get("href") or "").replace("&amp;", "&")
        if not magnet.startswith("magnet:") or magnet in seen:
            continue
        seen.add(magnet)
        params = parse_qs(urlparse(magnet).query)
        dn = (params.get("dn") or [""])[0]
        btih = (params.get("xt") or [""])[0].replace("urn:btih:", "")
        name = clean_magnet_name(dn) or link.get_text(strip=True) or btih
        out.append(_mk(name, magnet, "magnet", is_magnet=True, file_id=btih or None))
    return out


def _descriptive_name_before(link) -> str | None:
    """Older posts put 'Title (YYYY) ... - 8.6GB :' in the text right before
    the attachment link; the link text only has the quality tail."""
    node = link.find_previous(string=re.compile(r"\(\d{4}\)"))
    if node:
        text = re.sub(r"\s*:\s*$", "", str(node).strip())
        if re.search(r"\(\d{4}\)", text):
            return text
    return None


def parse_ipsattachlink(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Format 3 (older posts): a.ipsAttachLink / a[data-fileid] without
    data-fileext, href → attachment.php."""
    out, seen = [], set()
    for link in soup.select("a.ipsAttachLink, a[data-fileid]"):
        if link.get("data-fileext") == "torrent":
            continue  # handled by format 1
        href = link.get("href") or ""
        if "attachment.php" not in href and "/file/" not in href:
            continue
        link_text = link.get("title") or link.get_text(strip=True)
        name = _descriptive_name_before(link) or link_text
        if not name:
            continue
        if not href.startswith("http"):
            href = urljoin(base_url, href)
        href = href.replace("&amp;", "&")
        if href in seen:
            continue
        seen.add(href)
        item = _mk(name, href, "ipsAttachLink", file_id=link.get("data-fileid"))
        # quality info may live in the link tail rather than the title line
        tail_info = parse_torrent_name(f"{name} {link_text}")
        for k in ("quality", "codec", "rip_type", "file_size"):
            item[k] = item[k] or tail_info[k]
        out.append(item)
    return out


def extract_torrents(soup: BeautifulSoup, base_url: str) -> list[dict]:
    """Run the fallback chain on an already-fetched post page."""
    for fn, label in ((parse_magnets, "magnet"),
                      (parse_fileext, "fileext"),
                      (parse_ipsattachlink, "ipsAttachLink")):
        torrents = fn(soup, base_url)
        if torrents:
            log.debug(f"Torrent extraction: {len(torrents)} via {label}", url=base_url)
            return torrents
    log.warning("No torrents found in post", url=base_url)
    return []
