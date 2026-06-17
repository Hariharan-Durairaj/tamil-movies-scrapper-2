"""Runtime settings stored in the DB, editable from the web UI."""
from sqlalchemy.orm import Session

from .models import Setting

DEFAULTS: dict[str, str] = {
    # Forum
    "website_base": "www.1tamilmv",
    "current_domain": "www.1tamilmv.cards",
    "forum_path": "/index.php?/forums/forum/11-web-hd-itunes-hd-bluray/",
    "search_path": "/index.php?/search/&q={query}&quick=1",
    # New JSON search API (priority posts first). {query} and {page} substituted.
    "search_api_path": "/search/api/search.php?q={query}&priority=1&sort=date_desc&per_page=25&page={page}",
    "topic_path": "/index.php?/forums/topic/{tid}-x/",

    # Fingerprints used to verify the site after a domain change (ANY one match
    # is enough). Comma-separated, editable without redeploying.
    "site_fingerprints": "G-15B7F5LNBT,t.me/tmvog,data-focus-cookie='34'",

    # Download preferences
    "preferred_quality": "1080p",
    "preferred_codec": "x264",
    "rating_threshold": "6.5",
    "max_size_gb": "0",                      # 0 = no limit
    # When false, non-Tamil-original films (Indian movies with a Tamil dub) are
    # rejected in the auto-scan instead of being sent to Radarr.
    "allow_tamil_dubs": "false",

    # Metadata matching
    "match_auto_accept": "0.75",             # >= : auto accept
    "match_review_floor": "0.40",            # >= : needs_review, below: unmatched
    "tmdb_api_key": "",
    "omdb_api_key": "",

    # Integrations
    "radarr_url": "",
    "radarr_api_key": "",
    "radarr_quality_profile_id": "1",
    "radarr_root_folder": "",
    "qbittorrent_url": "",
    "qbittorrent_username": "",
    "qbittorrent_password": "",
    "qbittorrent_category": "radarr",

    # Radarr library sync (dedupe: never download what Radarr already has)
    "radarr_sync_enabled": "true",
    "radarr_sync_time": "05:45",

    # Daily scan
    "daily_scan_enabled": "true",
    "daily_scan_time": "06:00",
    "scan_pages": "3",
    "scan_max_links": "50",
    "duplicate_stop_count": "5",
    "auto_download": "true",

    # Full library scan
    "full_scan_last_page": "0",
    "full_scan_delay_seconds": "3",          # politeness delay between posts

    # Domain check
    "domain_check_enabled": "true",
    "domain_check_time": "05:30",

    # Housekeeping
    "log_retention_days": "30",
}


def ensure_defaults(session: Session) -> None:
    existing = {s.key for s in session.query(Setting.key).all()}
    for key, value in DEFAULTS.items():
        if key not in existing:
            session.add(Setting(key=key, value=value))


def get(session: Session, key: str, default: str | None = None) -> str | None:
    row = session.get(Setting, key)
    if row is not None and row.value is not None:
        return row.value
    return default if default is not None else DEFAULTS.get(key)


def get_bool(session: Session, key: str) -> bool:
    return (get(session, key) or "").strip().lower() in ("true", "1", "yes", "on")


def get_int(session: Session, key: str) -> int:
    try:
        return int(float(get(session, key) or 0))
    except (TypeError, ValueError):
        return 0


def get_float(session: Session, key: str) -> float:
    try:
        return float(get(session, key) or 0)
    except (TypeError, ValueError):
        return 0.0


def set_value(session: Session, key: str, value: str) -> None:
    row = session.get(Setting, key)
    if row:
        row.value = value
    else:
        session.add(Setting(key=key, value=value))


def all_settings(session: Session) -> dict[str, str]:
    out = dict(DEFAULTS)
    for s in session.query(Setting).all():
        out[s.key] = s.value if s.value is not None else ""
    return out


def base_url(session: Session) -> str:
    domain = (get(session, "current_domain") or "").strip().rstrip("/")
    if not domain.startswith("http"):
        domain = "https://" + domain
    return domain


def forum_url(session: Session) -> str:
    return base_url(session) + (get(session, "forum_path") or "")


def search_url(session: Session, query: str) -> str:
    from urllib.parse import quote
    path = (get(session, "search_path") or "").replace("{query}", quote(query))
    return base_url(session) + path


def search_api_url(session: Session, query: str, page: int = 1) -> str:
    from urllib.parse import quote_plus
    path = ((get(session, "search_api_path") or "")
            .replace("{query}", quote_plus(query))
            .replace("{page}", str(page)))
    return base_url(session) + path


def topic_url(session: Session, tid: int) -> str:
    path = (get(session, "topic_path") or "").replace("{tid}", str(tid))
    return base_url(session) + path


def fingerprints(session: Session) -> list[str]:
    raw = get(session, "site_fingerprints") or ""
    return [f.strip() for f in raw.split(",") if f.strip()]
