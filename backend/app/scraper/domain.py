"""Domain discovery for a site whose TLD keeps changing.

Chain (cheapest first):
  1. Stored current domain — still verifies? done.
  2. Domain history redirect-follow — old domains usually redirect to the new
     one; plain requests, no browser.
  3. Chrome search (undetected-chromedriver on Xvfb) across Google → DDG →
     Bing → Brave, following result redirects. Proven v1 method; headless /
     plain-HTTP search engines do not load results reliably.

Verification: ANY one fingerprint match in the page source is enough.
Fingerprints come from the DB so they can be updated from the UI.
"""
from __future__ import annotations

import time
import urllib.parse
from datetime import datetime, timezone

import requests

from .. import log
from ..db.models import DomainHistory
from .http import HEADERS

_RESULT_WAIT = 10

# ── selenium lazy import (heavy; only needed for step 3) ─────────────────
uc = None
By = WebDriverWait = EC = None


def _import_selenium() -> bool:
    global uc, By, WebDriverWait, EC
    if uc is not None:
        return True
    try:
        import undetected_chromedriver as _uc
        from selenium.webdriver.common.by import By as _By
        from selenium.webdriver.support.ui import WebDriverWait as _W
        from selenium.webdriver.support import expected_conditions as _EC
        uc, By, WebDriverWait, EC = _uc, _By, _W, _EC
        return True
    except Exception as e:
        log.warning(f"Selenium unavailable: {e}")
        return False


def _chromium_binary() -> str:
    import os
    for p in ("/usr/bin/chromium", "/usr/bin/chromium-browser",
              "/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"):
        if os.path.exists(p):
            return p
    return "chromium"


def _chromium_version() -> int | None:
    import re, subprocess
    try:
        out = subprocess.check_output([_chromium_binary(), "--version"], text=True)
        m = re.search(r"(\d+)\.", out)
        return int(m.group(1)) if m else None
    except Exception:
        return None


def _make_driver():
    """Headful Chrome on the Xvfb display (DISPLAY env, set by entrypoint)."""
    _import_selenium()
    options = uc.ChromeOptions()
    for arg in ("--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
                "--disable-extensions", "--window-size=1920,1080",
                "--disable-blink-features=AutomationControlled"):
        options.add_argument(arg)
    options.add_argument(f"--user-agent={HEADERS['User-Agent']}")
    return uc.Chrome(version_main=_chromium_version(),
                     browser_executable_path=_chromium_binary(), options=options)


# ── verification ─────────────────────────────────────────────────────────

def _domain_of(url: str) -> str | None:
    try:
        return urllib.parse.urlparse(url).netloc or None
    except Exception:
        return None


def verify_domain(domain: str, fingerprints: list[str], timeout: int = 20) -> str | None:
    """GET https://{domain}, follow redirects; if ANY fingerprint appears in
    the final page source, return the FINAL domain (post-redirect)."""
    url = domain if domain.startswith("http") else f"https://{domain}"
    try:
        resp = requests.get(url, headers=HEADERS, timeout=timeout, allow_redirects=True)
        html = resp.text
    except Exception as e:
        log.debug(f"verify_domain: {domain} unreachable: {e}")
        return None
    matches = [fp for fp in fingerprints if fp in html]
    if matches:
        final = _domain_of(resp.url) or domain
        log.debug(f"verify_domain: {domain} → {final} OK ({matches[0]!r})")
        return final
    log.debug(f"verify_domain: {domain} reachable but no fingerprint matched")
    return None


def _verify_with_chrome(driver, url: str, fingerprints: list[str]) -> str | None:
    if not url.startswith("http"):
        url = "https://" + url
    try:
        driver.get(url)
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((By.TAG_NAME, "body")))
    except Exception as e:
        log.debug(f"chrome verify: load failed {url}: {e}")
        return None
    final = _domain_of(driver.current_url)
    if any(fp in driver.page_source for fp in fingerprints):
        return final
    return None


# ── search collectors (ported from v1, proven) ───────────────────────────

def _collect(driver, engine: str, query: str, base_name: str, limit: int) -> list[str]:
    from bs4 import BeautifulSoup
    q = urllib.parse.quote_plus(query)
    urls = {
        "google": f"https://www.google.com/search?q={q}&hl=en",
        "duckduckgo": f"https://duckduckgo.com/?q={q}&ia=web",
        "bing": f"https://www.bing.com/search?q={q}",
        "brave": f"https://search.brave.com/search?q={q}&source=web",
    }
    waits = {
        "google": "div#search a[jsname]",
        "duckduckgo": "a[data-testid='result-title-a']",
        "bing": "li.b_algo h2 a",
        "brave": "div.snippet a.heading-serpresult",
    }
    driver.get(urls[engine])
    try:
        WebDriverWait(driver, _RESULT_WAIT).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, waits[engine])))
    except Exception:
        time.sleep(3)

    soup = BeautifulSoup(driver.page_source, "html.parser")
    results: list[str] = []

    def _unwrap(href: str) -> str | None:
        if not href:
            return None
        # Google /url?q= wrapper
        if "google." in driver.current_url and "/url?" in href:
            full = href if href.startswith("http") else "https://google.com" + href
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(full).query)
            href = (qs.get("q") or [href])[0]
        # DDG /l/?uddg= wrapper
        if "duckduckgo.com/l/" in href:
            full = "https:" + href if href.startswith("//") else href
            qs = urllib.parse.parse_qs(urllib.parse.urlparse(full).query)
            href = (qs.get("uddg") or [None])[0] or ""
        return href if href.startswith("http") else None

    blocked = ("google.", "bing.com", "microsoft.com", "brave.com", "duckduckgo.")
    for a in soup.select(waits[engine]) + soup.find_all("a", href=True):
        href = _unwrap(a.get("href", ""))
        if (href and base_name in href and href not in results
                and not any(b in href for b in blocked)):
            results.append(href)
        if len(results) >= limit:
            break
    return results


# ── public API ───────────────────────────────────────────────────────────

class DomainFinder:
    def __init__(self, website_base: str, fingerprints: list[str]):
        self.website_base = website_base                    # www.1tamilmv
        self.base_name = website_base.replace("www.", "").split(".")[0]
        self.fingerprints = fingerprints

    def check(self, domain: str) -> str | None:
        """Step 1+2 primitive: verify a single domain (follows redirects)."""
        return verify_domain(domain, self.fingerprints)

    def from_history(self, domains: list[str]) -> str | None:
        """Step 2: try known old domains; redirects reveal the new one."""
        for d in domains:
            found = verify_domain(d, self.fingerprints)
            if found:
                return found
        return None

    def from_chrome_search(self, max_candidates: int = 5) -> dict:
        """Step 3: Chrome search across engines + redirect-follow + verify.
        Returns {'verified': bool, 'domain': str|None, 'candidates': [...]}"""
        if not _import_selenium():
            return {"verified": False, "domain": None, "candidates": []}

        candidates: list[str] = []
        for engine in ("google", "duckduckgo", "bing", "brave"):
            if len(candidates) >= max_candidates:
                break
            driver = None
            try:
                log.info(f"Domain search via {engine}")
                driver = _make_driver()
                for url in _collect(driver, engine, self.website_base,
                                    self.base_name, max_candidates):
                    if url not in candidates:
                        candidates.append(url)
            except Exception as e:
                log.warning(f"Domain search {engine} failed: {e}")
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass

        if not candidates:
            return {"verified": False, "domain": None, "candidates": []}

        for url in candidates:
            driver = None
            try:
                driver = _make_driver()
                final = _verify_with_chrome(driver, url, self.fingerprints)
                if final:
                    log.info(f"Domain verified via Chrome: {final}")
                    return {"verified": True, "domain": final, "candidates": candidates}
            except Exception as e:
                log.warning(f"Chrome verification failed for {url}: {e}")
            finally:
                if driver:
                    try:
                        driver.quit()
                    except Exception:
                        pass
        return {"verified": False, "domain": None, "candidates": candidates}


def record_domain(session, domain: str, is_current: bool = True) -> None:
    """Upsert into domain_history; mark as the single current domain."""
    if is_current:
        session.query(DomainHistory).update({DomainHistory.is_current: False})
    row = session.query(DomainHistory).filter_by(domain=domain).one_or_none()
    now = datetime.now(timezone.utc)
    if row:
        row.last_verified = now
        row.is_current = is_current
    else:
        session.add(DomainHistory(domain=domain, last_verified=now, is_current=is_current))


def ensure_current_domain(session, force_search: bool = False) -> str | None:
    """Full discovery chain. Updates settings + history. Returns the working
    domain or None."""
    from ..db import settings_store as st

    finder = DomainFinder(st.get(session, "website_base") or "www.1tamilmv",
                          st.fingerprints(session))

    if not force_search:
        current = st.get(session, "current_domain") or ""
        if current:
            found = finder.check(current)
            if found:
                record_domain(session, found)
                if found != current.replace("https://", "").replace("http://", ""):
                    st.set_value(session, "current_domain", found)
                    log.info(f"Domain updated via redirect: {current} → {found}")
                return found

        history = [d.domain for d in session.query(DomainHistory)
                   .order_by(DomainHistory.last_verified.desc().nullslast()).limit(10)]
        history = [d for d in history if d != current]
        found = finder.from_history(history)
        if found:
            st.set_value(session, "current_domain", found)
            record_domain(session, found)
            log.info(f"Domain recovered from history: {found}")
            return found

    result = finder.from_chrome_search()
    if result["verified"]:
        st.set_value(session, "current_domain", result["domain"])
        record_domain(session, result["domain"])
        return result["domain"]

    log.error("Domain discovery failed", candidates=result["candidates"])
    st.set_value(session, "domain_candidates", ",".join(result["candidates"]))
    return None
