import conftest  # noqa: F401  (sys.path setup)
from conftest import load_soup

import app.scraper.forum as forum


def test_page_url():
    base = "https://www.1tamilmv.cards/index.php?/forums/forum/11-web-hd-itunes-hd-bluray/"
    assert forum.page_url(base, 1) == base
    assert forum.page_url(base, 2) == base.rstrip("/") + "/page/2/"


def test_list_topics(monkeypatch):
    monkeypatch.setattr(forum, "fetch_soup",
                        lambda url, timeout=20: load_soup("forum_listing.html"))
    topics = forum.list_topics("https://www.1tamilmv.cards/x/")
    # profile link filtered out; comment-anchor and plain topic deduped
    assert len(topics) == 2
    assert topics[0]["text"].startswith("Theri (2016)")
    assert topics[1]["text"].startswith("Beast (2022)")
    assert all("/topic/" in t["href"] for t in topics)
    assert all("#" not in t["href"] for t in topics)


def test_total_pages(monkeypatch):
    monkeypatch.setattr(forum, "fetch_soup",
                        lambda url, timeout=20: load_soup("forum_listing.html"))
    assert forum.total_pages("https://x/") == 97
