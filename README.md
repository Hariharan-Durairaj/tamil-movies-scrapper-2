# 🎬 Tamil Movie Automator v2

Automated pipeline: scrape 1TamilMV → match metadata reliably → rating gate → Radarr + qBittorrent → Jellyfin. Rebuilt from scratch with a scoring-based metadata engine, automatic domain rediscovery, an explicit pipeline state machine, and a forum library scanner.

See `PLAN.md` for the full architecture.

## Features

**Daily auto-scan** — scans the newest forum pages on a schedule, matches each movie's metadata, downloads everything rated above your threshold (preferred quality, default 1080p) and records the rest as rejected so you can override with one click.

**Smart metadata matching** — candidates from TMDB + the IMDb suggestion API + OMDB are scored on title similarity, year, original language and forum-post evidence (advertised audio languages, cast names). The Tamil *Beast* beats the Hollywood *Beast*. Ambiguous matches land in a Review queue instead of being saved wrong. `is_tamil_original` separates real Tamil films from dubs.

**Forum search** — search the forum from the UI; only results that actually contain torrents are shown, each with its qualities; pick one to send to Radarr + qBittorrent.

**Forum library** — a resumable full-forum scan catalogs every movie (metadata, rating, poster, torrent variants) so you can browse and download the good ones later.

**Radarr library sync** — imports Radarr's library into the DB (scheduled daily + manual button) and a duplicate guard checks Radarr by tmdb/imdb id before every auto-download, so the same movie is never downloaded twice.

**Domain auto-discovery** — when the site's TLD changes: last known domain → redirect-follow over domain history → Chrome (Xvfb) search across Google/DDG/Bing/Brave with fingerprint verification (any 1 match). Runs on schedule and automatically when scans hit connection errors.

**Small fast posters** — posters are downscaled once to ~300px WebP (~15 KB) and served locally.

## Deploy (Proxmox / any Docker host)

```bash
git clone https://github.com/Hariharan-Durairaj/tamil-movies-scrapper-2.git
cd tamil-movies-scrapper-2
DB_PASSWORD=changeme docker compose up -d
```

Open `http://<host>:8586` (port 8586 — won't clash with the old project), then in **Settings** fill in:

1. `tmdb_api_key` (free at themoviedb.org) and optionally `omdb_api_key`
2. `radarr_url` + `radarr_api_key` (use **Test Radarr** — it lists your quality profile IDs)
3. `qbittorrent_url` + credentials (use **Test qBittorrent**)
4. Check `current_domain` is up to date, adjust `rating_threshold` / scan time

The GitHub Actions workflow builds and pushes the image to GHCR on every push to `main`; on the server `docker compose pull && docker compose up -d` updates it.

## Local development

```bash
pip install -r requirements.txt
cp .env.example .env          # point DB_HOST at a local postgres
cd backend
python -m uvicorn app.main:app --reload --port 8586
```

Run tests: `cd backend && python -m pytest tests/ -q`

## Project layout

```
backend/app/
  config.py            env config (DB, paths)
  db/                  models, session, runtime settings store
  scraper/             http session, parsers, torrent extraction (3 formats),
                       forum listing/search/pagination, domain discovery
  metadata/            tmdb / omdb / imdb clients, scoring engine, posters
  integrations/        radarr, qbittorrent
  pipeline/            processor (state machine), scanner (daily / full / search)
  scheduler.py         APScheduler jobs
  api/routes.py        REST API
backend/tests/         unit tests on saved forum HTML fixtures
frontend/              single-page UI (no build step)
```

## Movie statuses

`discovered → matched | needs_review | unmatched → qualified | rejected → sent | failed`, plus `library` for catalog-only entries. Every transition is logged (Logs page). Rejected and needs-review movies keep a one-click "Download anyway".
