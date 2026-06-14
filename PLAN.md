# Tamil Movie Automator v2 — Architecture Plan

Rebuild of the 1TamilMV → Radarr → qBittorrent → Jellyfin pipeline. Goals: highly automated, smart, reliable. This plan fixes the weak points of v1 (wrong-language metadata matches, heavy Selenium domain discovery, fragile monolithic processor) while keeping what worked (the three torrent-format parsers, fingerprint domain verification, duplicate-stop scanning, resumable full scan).

## 1. Stack

- **Backend**: Python 3.12, FastAPI, SQLAlchemy 2 + Alembic migrations, APScheduler (replaces `schedule` + thread), httpx with retries, Pydantic Settings for config.
- **DB**: PostgreSQL 16 (Docker).
- **Frontend**: single-page vanilla JS + HTML/CSS (dark theme like v1), served by FastAPI. No build step — keeps the Docker image simple.
- **Deployment**: GitHub repo → GitHub Actions builds image → GHCR → docker-compose on Proxmox.

## 2. Module layout

```
backend/
  app/
    config.py            # Pydantic settings (.env + DB-stored settings)
    db/                  # models, session, migrations (Alembic)
    scraper/
      domain.py          # domain discovery + verification
      forum.py           # listing pages, post pages, pagination
      torrents.py        # 3-format torrent extraction (fileext / magnet / ipsAttachLink)
      parse.py           # title/year/quality/codec/size/language-tag parsing
    metadata/
      engine.py          # candidate gathering + scoring (the fix for "Beast")
      tmdb.py, omdb.py, imdb.py   # source clients
    integrations/
      radarr.py, qbittorrent.py
    pipeline/
      processor.py       # state machine: discover → match → qualify → send
      scanner.py         # daily scan + full forum library scan (resumable)
    scheduler.py         # APScheduler jobs
    api/                 # FastAPI routers: movies, search, library, settings, logs, auth
  tests/
    fixtures/            # saved real forum HTML pages
    test_parse.py, test_torrents.py, test_metadata.py
frontend/
docker/                  # Dockerfile, entrypoint, compose
```

Every scraping/parsing function gets unit tests against saved real HTML fixtures — parsing is what breaks silently, so this is the reliability backbone.

## 3. Domain discovery (lighter + more reliable than v1)

Chain, cheapest first:

1. **Stored domain** — try the last known domain; if it responds and a fingerprint matches, done.
2. **Domain history redirect-follow** — keep a `domain_history` table of every domain ever seen. Old 1TamilMV domains usually redirect to the new one, so requesting recent old domains with `follow_redirects=True` often yields the new domain with zero search. No browser needed.
3. **Chrome search (proven v1 method)** — visible Chrome via undetected-chromedriver under Xvfb, searching Google → DuckDuckGo → Bing → Brave, following redirects from result links. Headless/plain-HTTP search was already tested in v1 and fails (results don't load, or wrong sites) — real Chrome is the reliable option, so it stays. Telegram channel scraping was evaluated and rejected: `t.me/s/tmvog` has web preview disabled and just redirects to the app prompt.

Every candidate is verified with the fingerprint check (Analytics ID, t.me/tmvog link, theme cookie) — **any 1 of 3 is enough**. Fingerprints are DB-configurable so they can be updated without redeploying. Domain check runs on a schedule and automatically when a scan fails with connection errors. All scraping requests go through one session wrapper with retry/backoff and an automatic "domain may have changed → rediscover → retry once" hook.

## 4. Metadata engine (the "Beast" fix)

Core change: v1 returned the *first* acceptable result from a source chain. v2 gathers **candidates from all sources, scores them, and refuses to guess when unsure**.

**Candidate sources**

- TMDB search (with year, without year) — full result list, not just first hit.
- IMDb **suggestion API** (`v2.sg.media-imdb.com/suggestion/...json`) — free, no key, JSON, far more reliable than scraping IMDb search pages. Rating + poster then come from the title page's JSON-LD. This covers small Tamil films missing from TMDB.
- OMDB as supplementary (ratings, language field).

**Scoring signals** (weighted, stored with the match)

- Title similarity (normalized fuzzy match, handles transliteration variants).
- Year proximity (forum year can be off by one).
- **Language**: `original_language == 'ta'` strong boost; other Indian languages medium (dub case); Western languages strong penalty.
- **Forum-post evidence**: torrent names carry language tags (`Tamil`, `[Tamil + Telugu + Hindi]`, `HQ Clean Aud`); posts often name cast/director — cross-checked against TMDB/IMDb credits when available. This is what disambiguates Vijay's *Beast* from the Idris Elba *Beast*.

**Outcomes**

- Score ≥ high threshold → auto-accept, store `match_confidence`.
- Between thresholds → store best candidate but status `needs_review`; review queue in UI shows top 3 candidates side by side, one click to pick.
- No candidate → `unmatched`, still cataloged with forum data.

**Tamil filter**: `is_tamil_original` boolean column (original_language = ta), exposed as a library filter — actual Tamil films vs. dubs, per your requirement.

**Caching**: `metadata_lookups` table caches every external query so re-scans and the full library scan don't hammer APIs.

**Posters (small + fast)**: v1 stored full-size poster URLs — heavy pages, lots of data. v2 downloads each poster once, resizes to ~300px wide WebP at ~70 quality (≈10–20 KB each vs. several hundred KB), stores it on disk, and serves it from the backend (`/posters/{movie_id}.webp`). TMDB is requested at `w342` size and IMDb URLs use their built-in resize parameter, so even the one-time download is small. Library pages load instantly and work offline from the forum/APIs.

**Radarr nuance**: Radarr can only add movies that exist in TMDB. For IMDb-only matches, the torrent still goes to qBittorrent with a category/save-path Jellyfin picks up, and the movie is flagged `radarr_skipped: not_in_tmdb` so you can see it.

## 5. Pipeline (explicit state machine)

Each movie row carries a `status`: `discovered → matched | needs_review | unmatched → qualified | rejected(reason) → sent_to_radarr → sent_to_qbittorrent → downloading → completed | failed`. Every transition is logged with context. This replaces v1's 350-line `process_movie()` and makes "why wasn't this downloaded?" answerable from the UI.

Daily auto-scan (kept from v1, with fixes): scan forum listing pages → parse title/year → skip duplicates (consecutive-duplicate stop) → metadata match → rating ≥ threshold? → pick best 1080p torrent (quality ranking: preferred quality > preferred codec > sane size) → add to Radarr (by tmdb_id) → send torrent/magnet to qBittorrent → record in DB. Below-threshold movies are stored as `rejected` with the rating, and the UI has a one-click "download anyway" that runs the same send path.

## 6. Features mapped to your requirements

- **Auto-scan with rating threshold** → pipeline above, APScheduler daily job, configurable time/pages/threshold.
- **Manual search** → forum search → filter results to posts that actually yield torrents (run the 3-format extractor; drop link-only posts) → show qualities → you pick → same send path.
- **Forum library** → resumable full-forum scan (page checkpoint in DB like v1), rate-limited background job with progress UI; catalogs every movie with metadata, qualities, posters; filterable (Tamil-original / dub / rating / year / downloaded).
- **Review database** → review queue (low-confidence matches) + rejected list (below threshold) with override actions.

## 7. Web UI pages

Dashboard (stats, recent activity, scan controls) · Library (filters incl. Tamil-original) · Search · Review queue · Movie detail (status timeline, candidates, qualities, actions) · Settings · Logs. Single admin login (bcrypt + JWT, as v1).

## 8. Deployment

- Two-service docker-compose: `backend` + `postgres`. Chrome + Xvfb stay in the image (needed for reliable domain discovery), with the v1 `shm_size`/`SYS_ADMIN` compose settings carried over.
- Config via `.env` (DB creds, API keys) + runtime settings in DB (URLs, thresholds, schedule).
- GitHub Actions: on push to `main`, run tests → build → push to GHCR. On Proxmox: `docker compose pull && up -d` (Watchtower optional for full auto-update).
- Healthcheck endpoint, structured logs to DB + stdout.

## 9. Build phases

| Phase | Deliverable |
|---|---|
| 1 | Skeleton: config, DB models + Alembic, domain discovery, forum/torrent scrapers + parser tests on real HTML fixtures |
| 2 | Metadata engine with scoring, caching, review statuses + tests |
| 3 | Pipeline: processor state machine, Radarr/qBittorrent clients, daily scan scheduler |
| 4 | API + Web UI (all pages) |
| 5 | Forum library full scan (resumable) + library filters |
| 6 | Dockerfile, compose, GitHub Actions, README, deploy guide for Proxmox |

Each phase ends runnable and tested before the next starts.

## 10. Open questions

1. Forum section: v1 scanned forum/14 — confirm that's still the right section for new Tamil releases.
2. Auth: keep single-admin login, or is it LAN-only and you'd rather skip login?
