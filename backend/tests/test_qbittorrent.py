"""Regression tests for QBittorrentClient._add_ok response parsing."""
from types import SimpleNamespace

from app.integrations.qbittorrent import QBittorrentClient


def _resp(status, text):
    import json as _json

    def _json_fn():
        return _json.loads(text)

    return SimpleNamespace(status_code=status, text=text, json=_json_fn)


def test_202_pending_is_success():
    # qBittorrent v5 answers a URL add with HTTP 202 + pending_count. This is
    # the exact body that previously logged "qbittorrent_failed".
    r = _resp(202, '{"added_torrent_ids":[],"failure_count":0,'
                   '"pending_count":1,"success_count":0}')
    assert QBittorrentClient._add_ok(r) is True


def test_200_success_count():
    r = _resp(200, '{"added_torrent_ids":["abc"],"failure_count":0,'
                   '"success_count":1}')
    assert QBittorrentClient._add_ok(r) is True


def test_200_plaintext_ok():
    r = _resp(200, "Ok.")
    assert QBittorrentClient._add_ok(r) is True


def test_all_fail_is_failure():
    r = _resp(200, '{"added_torrent_ids":[],"failure_count":1,'
                   '"pending_count":0,"success_count":0}')
    assert QBittorrentClient._add_ok(r) is False


def test_error_status_is_failure():
    r = _resp(403, "Forbidden")
    assert QBittorrentClient._add_ok(r) is False
