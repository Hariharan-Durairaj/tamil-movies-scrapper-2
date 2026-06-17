"""Tests for the qBittorrent delivery chain in processor._deliver_to_qbittorrent.

Rules under test:
  1. magnet → handed to qBittorrent as a URL (resolved via DHT/trackers)
  2. else → download the .torrent file ourselves and upload the bytes
  3. download failure → fetch a fresh link from the forum, download, upload
  4. an http .torrent link is NEVER handed to qBittorrent to fetch itself
"""
from app.db.models import Movie, MovieTorrent
from app.pipeline import processor


class FakeQB:
    def __init__(self, file_ok=True, url_ok=True):
        self.file_ok, self.url_ok = file_ok, url_ok
        self.url_calls, self.file_calls = [], []

    def add_torrent_url(self, url, category="radarr"):
        self.url_calls.append(url)
        return self.url_ok

    def add_torrent_file(self, path, category="radarr"):
        self.file_calls.append(path)
        return self.file_ok


def _patch_download(monkeypatch, side_effect):
    monkeypatch.setattr(processor, "download_file", side_effect)
    monkeypatch.setattr(processor, "_is_torrent_file", lambda p: True)


def test_magnet_sent_as_url_no_file():
    qb = FakeQB()
    t = MovieTorrent(name="X", torrent_url="magnet:?xt=urn:btih:abc", is_magnet=True)
    m = Movie(title="X")
    assert processor._deliver_to_qbittorrent(None, qb, m, t, "radarr") is True
    assert qb.url_calls == ["magnet:?xt=urn:btih:abc"]
    assert qb.file_calls == []


def test_attachment_downloads_and_uploads_file(monkeypatch):
    qb = FakeQB()
    _patch_download(monkeypatch, lambda url, path, referer=None: path)
    t = MovieTorrent(name="X", torrent_url="https://s/attachment.php?id=1",
                     is_magnet=False)
    m = Movie(title="X", forum_url="https://s/topic/1")
    assert processor._deliver_to_qbittorrent(None, qb, m, t, "radarr") is True
    assert len(qb.file_calls) == 1
    # the http link is never handed to qBittorrent to fetch
    assert qb.url_calls == []


def test_download_failure_uses_fresh_link(monkeypatch):
    qb = FakeQB()
    calls = {"n": 0}

    def dl(url, path, referer=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("400 Bad Request")
        return path

    _patch_download(monkeypatch, dl)
    monkeypatch.setattr(processor, "_fresh_link_from_forum",
                        lambda s, m, t: {"is_magnet": False,
                                         "torrent_url": "https://new/attachment.php?id=9"})
    t = MovieTorrent(name="X", torrent_url="https://old/attachment.php?id=1",
                     is_magnet=False)
    m = Movie(title="X", forum_url="https://old/topic/1")
    assert processor._deliver_to_qbittorrent(None, qb, m, t, "radarr") is True
    assert calls["n"] == 2          # retried after fetching a fresh link
    assert len(qb.file_calls) == 1
    assert qb.url_calls == []       # never asked qB to fetch an http url


def test_never_hands_http_url_to_qb_even_on_total_failure(monkeypatch):
    qb = FakeQB(file_ok=False)
    _patch_download(monkeypatch, lambda url, path, referer=None: path)
    monkeypatch.setattr(processor, "_fresh_link_from_forum", lambda s, m, t: None)
    t = MovieTorrent(name="X", torrent_url="https://old/attachment.php?id=1",
                     is_magnet=False)
    m = Movie(title="X", forum_url="https://old/topic/1")
    assert processor._deliver_to_qbittorrent(None, qb, m, t, "radarr") is False
    assert qb.url_calls == []


def test_magnet_failure_falls_back_to_fresh_link(monkeypatch):
    # A magnet add can fail; per the rules we then try a .torrent file. A magnet
    # has no http url of its own, so we go straight to a fresh forum link.
    qb = FakeQB(url_ok=False)
    _patch_download(monkeypatch, lambda url, path, referer=None: path)
    monkeypatch.setattr(processor, "_fresh_link_from_forum",
                        lambda s, m, t: {"is_magnet": False,
                                         "torrent_url": "https://new/attachment.php?id=9"})
    t = MovieTorrent(name="X", torrent_url="magnet:?xt=urn:btih:abc", is_magnet=True)
    m = Movie(title="X", forum_url="https://s/topic/1")
    assert processor._deliver_to_qbittorrent(None, qb, m, t, "radarr") is True
    assert len(qb.file_calls) == 1
