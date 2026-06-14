"""The 'Beast' test: the Tamil film must outscore the Hollywood one."""
import conftest  # noqa: F401

from app.metadata.engine import score_candidate

TAMIL_BEAST = {
    "source": "tmdb", "tmdb_id": 1, "title": "Beast",
    "original_title": "பீஸ்ட்", "year": 2022,
    "original_language": "ta", "rating": 5.3, "votes": 250,
}
ENGLISH_BEAST = {
    "source": "tmdb", "tmdb_id": 2, "title": "Beast",
    "original_title": "Beast", "year": 2022,
    "original_language": "en", "rating": 7.0, "votes": 50000,
}


def test_tamil_beast_outscores_english_beast():
    forum_langs = ["tamil", "telugu", "hindi"]
    s_ta = score_candidate(TAMIL_BEAST, "Beast", 2022, forum_langs)
    s_en = score_candidate(ENGLISH_BEAST, "Beast", 2022, forum_langs)
    assert s_ta > s_en, f"tamil={s_ta} should beat english={s_en}"


def test_year_mismatch_penalised():
    right = dict(TAMIL_BEAST)
    wrong = dict(TAMIL_BEAST, year=2010)
    assert score_candidate(right, "Beast", 2022, ["tamil"]) > \
           score_candidate(wrong, "Beast", 2022, ["tamil"])


def test_title_similarity_dominates_garbage():
    other = dict(TAMIL_BEAST, title="Completely Other Film", original_title=None)
    assert score_candidate(TAMIL_BEAST, "Beast", 2022, ["tamil"]) > \
           score_candidate(other, "Beast", 2022, ["tamil"]) + 0.3


def test_omdb_language_names_used():
    omdb_tamil = {"source": "omdb", "title": "Beast", "original_title": "Beast",
                  "year": 2022, "original_language": None,
                  "language_names": "tamil, telugu", "rating": 5.4, "votes": 0}
    s = score_candidate(omdb_tamil, "Beast", 2022, ["tamil"])
    s_en = score_candidate(ENGLISH_BEAST, "Beast", 2022, ["tamil"])
    assert s > s_en
