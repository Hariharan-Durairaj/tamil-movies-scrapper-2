"""Poster handling: download once, shrink to ~300px WebP (~10-20 KB),
serve locally from /posters/. Keeps library pages fast."""
from __future__ import annotations

import io

import requests
from PIL import Image

from .. import log
from ..config import env
from ..scraper.http import HEADERS

MAX_WIDTH = 300
WEBP_QUALITY = 70


def save_poster(url: str | None, movie_id: int) -> str | None:
    """Download, downscale and store a poster. Returns the local filename
    (relative, e.g. '123.webp') or None."""
    if not url:
        return None
    try:
        r = requests.get(url, headers=HEADERS, timeout=30)
        r.raise_for_status()
        img = Image.open(io.BytesIO(r.content)).convert("RGB")
        if img.width > MAX_WIDTH:
            ratio = MAX_WIDTH / img.width
            img = img.resize((MAX_WIDTH, max(1, int(img.height * ratio))),
                             Image.LANCZOS)
        filename = f"{movie_id}.webp"
        img.save(env.posters_dir / filename, "WEBP", quality=WEBP_QUALITY)
        return filename
    except Exception as e:
        log.warning(f"Poster download failed for movie {movie_id}: {e}", url=url)
        return None
