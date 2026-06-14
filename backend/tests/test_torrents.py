from conftest import load_soup

from app.scraper.torrents import extract_torrents

BASE = "https://www.1tamilmv.cards/index.php?/forums/topic/1-test/"


def test_fileext_format_wins_over_magnet():
    torrents = extract_torrents(load_soup("post_fileext.html"), BASE)
    assert len(torrents) == 2
    assert all(t["source_format"] == "fileext" for t in torrents)
    t720, t1080 = torrents
    assert t720["quality"] == "720p" and t720["file_size"] == "1.2GB"
    assert t1080["quality"] == "1080p" and t1080["codec"] == "HEVC"
    # relative href resolved, &amp; unescaped
    assert t1080["torrent_url"].startswith("https://www.1tamilmv.cards/")
    assert "&key=" in t1080["torrent_url"]
    assert "tamil" in t720["languages"]


def test_magnet_fallback():
    torrents = extract_torrents(load_soup("post_magnet.html"), BASE)
    assert len(torrents) == 1
    t = torrents[0]
    assert t["source_format"] == "magnet" and t["is_magnet"]
    assert t["torrent_url"].startswith("magnet:?xt=urn:btih:ffad91b2c3")
    assert t["name"].startswith("Bairavan (2025)")
    assert t["quality"] == "720p" and t["file_size"] == "1.2GB"


def test_ipsattachlink_fallback_uses_descriptive_line():
    torrents = extract_torrents(load_soup("post_ipsattach.html"), BASE)
    assert len(torrents) == 1
    t = torrents[0]
    assert t["source_format"] == "ipsAttachLink"
    assert t["name"].startswith("ASURAGURU (2020)")
    assert t["quality"] == "1080p" and t["file_size"] == "8.6GB"
    assert t["rip_type"] == "WEB-DL"
