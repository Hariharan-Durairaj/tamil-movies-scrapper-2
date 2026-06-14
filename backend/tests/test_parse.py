from app.scraper.parse import (clean_magnet_name, detect_languages,
                               file_size_gb, parse_movie_title_year,
                               parse_torrent_name, title_similarity)


def test_parse_torrent_name_full():
    info = parse_torrent_name(
        "Theri (2016) Tamil TRUE WEB-DL - 1080p - x264 - (AAC 2.0) - 2.6GB")
    assert info == {"quality": "1080p", "codec": "x264",
                    "file_size": "2.6GB", "rip_type": "WEB-DL"}


def test_parse_torrent_name_variants():
    assert parse_torrent_name("Movie 4K HEVC BluRay 12GB")["quality"] == "4K"
    assert parse_torrent_name("Movie HQ HDRip 700MB")["rip_type"] == "HDRip"
    assert parse_torrent_name("Movie HDTC 720p")["rip_type"] == "HDTC"
    assert parse_torrent_name("nothing here") == {
        "quality": None, "codec": None, "file_size": None, "rip_type": None}


def test_file_size_gb():
    assert file_size_gb("2.6GB") == 2.6
    assert abs(file_size_gb("800MB") - 0.78125) < 1e-6
    assert file_size_gb(None) is None
    assert file_size_gb("junk") is None


def test_parse_title_year():
    assert parse_movie_title_year(
        "Theri (2016) Tamil TRUE WEB-DL - 1080p") == {"title": "Theri", "year": 2016}
    assert parse_movie_title_year("No Year Movie HDRip") == {
        "title": "No Year Movie HDRip", "year": None}
    # non-breaking space (common in magnet dn)
    assert parse_movie_title_year("Beast\xa0(2022) Tamil")["year"] == 2022


def test_detect_languages():
    assert detect_languages(
        "Beast (2022) Tamil HQ HDRip - [Tamil + Telugu + Hindi] - 700MB"
    ) == ["tamil", "telugu", "hindi"]
    assert detect_languages("Theri (2016) Tamil WEB-DL") == ["tamil"]
    assert detect_languages("Random Movie 1080p") == []


def test_clean_magnet_name():
    dn = "www.1TamilMV.buzz%20-%20Bairavan%20%282025%29%C2%A0Tamil%20HDRip.mkv"
    assert clean_magnet_name(dn).startswith("Bairavan (2025)")
    assert not clean_magnet_name(dn).endswith(".mkv")


def test_title_similarity():
    assert title_similarity("Beast", "Beast") == 1.0
    assert title_similarity("Theri", "Completely Different") < 0.5
    assert title_similarity("Jai Bhim", "Jai Bhim!") > 0.9
