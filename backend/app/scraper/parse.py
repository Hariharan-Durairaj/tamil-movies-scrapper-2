"""Pure text parsers for forum topic titles and torrent release names.
No network access — fully unit-testable."""
from __future__ import annotations

import re
import unicodedata
from urllib.parse import unquote

# ── Rip type (most specific / best quality first) ────────────────────────
RIP_PATTERNS: list[tuple[str, str]] = [
    (r"\bBlu[- ]?Ray\b|\bBDRip\b|\bBRRip\b", "BluRay"),
    (r"\b(?:TRUE\s+)?WEB[- ]?DL\b", "WEB-DL"),
    (r"\bWEB[- ]?Rip\b", "WEBRip"),
    (r"\bHQ[- ]?(?:HD)?Rip\b|\bHD[- ]?Rip\b", "HDRip"),
    (r"\bDVD[- ]?Rip\b", "DVDRip"),
    (r"\bHDTV[- ]?Rip\b|\bHDTV\b", "HDTV"),
    (r"\bHD[- ]?TC\b|\bHDTC\b", "HDTC"),
    (r"\bPre[- ]?DVD\b|\bDVD[- ]?Scr\b", "PreDVD"),
    (r"\bHQ[- ]?CAM\b|\bCAM\b|\bTS\b", "CAM/TS"),
]

LANGUAGE_WORDS = {
    "tamil": "tamil", "tam": "tamil",
    "telugu": "telugu", "tel": "telugu",
    "hindi": "hindi", "hin": "hindi",
    "malayalam": "malayalam", "mal": "malayalam",
    "kannada": "kannada", "kan": "kannada",
    "english": "english", "eng": "english",
    "korean": "korean", "kor": "korean",
    "japanese": "japanese", "jap": "japanese", "jpn": "japanese",
    "chinese": "chinese", "chi": "chinese",
}


def parse_torrent_name(name: str) -> dict:
    """Extract quality / codec / rip type / file size from a release name.
    Example: 'Theri (2016) Tamil TRUE WEB-DL - 1080p - x264 - 2.6GB'."""
    info = {"quality": None, "codec": None, "file_size": None, "rip_type": None}

    for pattern, label in RIP_PATTERNS:
        if re.search(pattern, name, re.IGNORECASE):
            info["rip_type"] = label
            break

    m = re.search(r"\b(4K|2160p|1080p|720p|480p)\b", name, re.IGNORECASE)
    if not m:
        m = re.search(r"\b(UHD|FHD|HD)\b", name)
    if m:
        info["quality"] = m.group(1)

    m = re.search(r"\b(HEVC|AVC|x264|x265|H\.?264|H\.?265)\b", name, re.IGNORECASE)
    if m:
        info["codec"] = m.group(1)

    sizes = re.findall(r"\b(\d+(?:\.\d+)?\s?(?:GB|MB|TB))\b", name, re.IGNORECASE)
    if sizes:
        info["file_size"] = sizes[0].replace(" ", "")

    return info


def file_size_gb(size_str: str | None) -> float | None:
    """'1.4GB' -> 1.4, '800MB' -> 0.78. None if unparseable."""
    if not size_str:
        return None
    m = re.match(r"(\d+(?:\.\d+)?)\s?(GB|MB|TB)", size_str.strip(), re.IGNORECASE)
    if not m:
        return None
    val, unit = float(m.group(1)), m.group(2).upper()
    return val * {"MB": 1 / 1024, "GB": 1, "TB": 1024}[unit]


def parse_movie_title_year(text: str) -> dict:
    """'Theri (2016) Tamil TRUE WEB-DL...' -> {'title': 'Theri', 'year': 2016}."""
    text = text.replace("\xa0", " ").strip()
    m = re.search(r"^(.+?)\s*\((\d{4})\)", text)
    if m:
        return {"title": m.group(1).strip(), "year": int(m.group(2))}
    m = re.search(r"\((\d{4})\)", text)
    if m:
        return {"title": text[: m.start()].strip(), "year": int(m.group(1))}
    return {"title": text, "year": None}


def detect_languages(text: str) -> list[str]:
    """Find the audio languages advertised in a forum/torrent title.

    Handles '[Tamil + Telugu + Hindi]', 'Tamil HDRip', '(Tam + Tel)' etc.
    Returns normalized names in order of appearance, deduplicated.
    """
    found: list[str] = []
    # Bracketed language blocks are the strongest signal
    for block in re.findall(r"[\[(]([^\])]{3,80})[\])]", text):
        for raw in re.split(r"[+,/&]", block):
            w = raw.strip().lower()
            if w in LANGUAGE_WORDS and LANGUAGE_WORDS[w] not in found:
                found.append(LANGUAGE_WORDS[w])
    # Free-standing language words
    for m in re.finditer(r"[A-Za-z]+", text):
        w = m.group(0).lower()
        if w in LANGUAGE_WORDS and len(w) > 3 and LANGUAGE_WORDS[w] not in found:
            found.append(LANGUAGE_WORDS[w])
    return found


def clean_magnet_name(dn: str) -> str:
    """Magnet dn → clean release name: drop 'www.site.tld - ' prefix,
    container extension, and non-breaking spaces."""
    name = unquote(dn or "")
    name = name.replace("\xa0", " ")
    name = re.sub(r"^\s*www\.\S+\s*-\s*", "", name, flags=re.IGNORECASE)
    name = re.sub(r"\.(mkv|mp4|avi)\s*$", "", name, flags=re.IGNORECASE)
    return name.strip()


def normalize_title(title: str) -> str:
    """Normalize for fuzzy comparison: lowercase, strip accents,
    drop punctuation, collapse whitespace."""
    t = unicodedata.normalize("NFKD", title or "")
    t = "".join(c for c in t if not unicodedata.combining(c))
    t = t.lower()
    t = re.sub(r"[^a-z0-9\s]", " ", t)
    return re.sub(r"\s+", " ", t).strip()


def title_similarity(a: str, b: str) -> float:
    """0..1 similarity between two titles (normalized SequenceMatcher)."""
    from difflib import SequenceMatcher
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    return SequenceMatcher(None, na, nb).ratio()
