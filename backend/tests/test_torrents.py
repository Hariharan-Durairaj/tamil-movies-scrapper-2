from conftest import load_soup

from app.scraper.torrents import extract_torrents

BASE = "https://www.1tamilmv.cards/index.php?/forums/topic/1-test/"


def test_magnet_wins_over_fileext():
    # post_fileext.html has both a magnet and 4 attachment links; the magnet
    # is now preferred so qBittorrent gets a URL it can actually fetch.
    torrents = extract_torrents(load_soup("post_fileext.html"), BASE)
    assert len(torrents) == 1
    t = torrents[0]
    assert t["source_format"] == "magnet" and t["is_magnet"]
    assert t["torrent_url"].startswith("magnet:?xt=urn:btih:ffad91aaaa")


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
