"""Hybrid memory search: keywords (FTS5) + meaning (vectors) + RRF, date and category hints."""
from datetime import date, datetime, timedelta, timezone

import pytest

import query_hints
from conftest import FakeEmbedder
from memory import Memory, rrf

IST = timezone(timedelta(hours=5, minutes=30))
NOW = datetime(2026, 9, 28, 18, 0, tzinfo=IST)  # a Monday
DAY = timedelta(days=1)


class BilingualEmbedder(FakeEmbedder):
    """Stands in for the multilingual e5 model: Hindi words mean the same as English ones."""
    SAME = {"एलर्जी": "allergy", "मूंगफली": "peanut", "गाड़ी": "car"}

    def _vec(self, text):
        return super()._vec(" ".join(self.SAME.get(w, w) for w in text.split()))

    embed_query = _vec
    embed_passage = _vec


def test_rrf_prefers_items_found_by_both_searches():
    scores = rrf(["a", "b", "c"], ["c", "d"])
    assert max(scores, key=scores.get) == "c"
    assert scores["a"] > scores["d"]


def test_keyword_only_match_finds_names_and_numbers(embedder):
    m = Memory(embedder)
    m.add_turn("Rohan fixed the washing machine and the dryer on the weekend", "Great.", "en", NOW - 9 * DAY)
    m.add_turn("what is the capital of france", "Paris.", "en", NOW - 8 * DAY)
    m.add_turn("the invoice number was 48213 for the new sofa", "Noted.", "en", NOW - 7 * DAY)
    # Too few shared words for the meaning search (threshold 0.8) ...
    assert m.search("who is rohan", k=3, min_similarity=0.8, hints=query_hints.Hints()) == []
    # ... but the keyword index finds the name and the number.
    [hit] = m.search("who is rohan", k=3, min_similarity=0.8)
    assert "Rohan fixed" in hit
    [hit] = m.search("which purchase had 48213", k=3, min_similarity=0.8)
    assert "48213" in hit


def test_prefix_terms_match_word_forms(embedder):
    m = Memory(embedder)
    m.add_turn("I think I have a peanut allergy", "Okay.", "en", NOW - 3 * DAY)
    [hit] = m.search("am i allergic to peanuts", k=3, min_similarity=0.99)
    assert "peanut allergy" in hit


def test_cross_language_match_through_meaning():
    m = Memory(BilingualEmbedder())
    m.add_turn("peanut allergy is serious for me", "I'll keep it in mind.", "en", NOW - 5 * DAY)
    m.add_turn("what time is it", "Six.", "en", NOW - 4 * DAY)
    [hit] = m.search("मूंगफली एलर्जी", k=3, min_similarity=0.35)
    assert "peanut allergy" in hit


def test_hindi_words_are_indexed_whole(embedder):
    m = Memory(embedder)
    m.add_turn("मुझे मूंगफली से एलर्जी है", "ध्यान रखूँगा।", "hi", NOW - 2 * DAY)
    [hit] = m.search("एलर्जी", k=3, min_similarity=0.99)
    assert "मूंगफली" in hit


def test_date_question_returns_that_days_turns(embedder):
    m = Memory(embedder)
    m.add_turn("I went to the dentist", "Hope it went well.", "en", NOW - 1 * DAY)
    m.add_turn("book tickets for the concert", "Done looking.", "en", NOW - 1 * DAY)
    m.add_turn("I went to the gym", "Nice.", "en", NOW - 3 * DAY)
    m.add_turn("hello", "Hi.", "en", NOW)
    hints = query_hints.parse("what did we talk about yesterday?", NOW)
    found = m.search("what did we talk about yesterday?", k=5, min_similarity=0.8, hints=hints)
    assert len(found) == 2 and all("2026-09-27" in f for f in found)
    assert "dentist" in found[0] and "concert" in found[1]  # in the order they happened


def test_date_filter_limits_keyword_hits(embedder):
    m = Memory(embedder)
    m.add_turn("the dentist said my teeth are fine", "Good.", "en", NOW - 40 * DAY)
    m.add_turn("dentist appointment moved to friday", "Okay.", "en", NOW - 5 * DAY)
    hints = query_hints.parse("what did the dentist say last week", NOW)
    found = m.search("what did the dentist say last week", k=5, min_similarity=0.8, hints=hints)
    assert len(found) == 1 and "friday" in found[0]


def test_recent_turns_rank_higher_when_equally_related(embedder):
    m = Memory(embedder)
    m.add_turn("my favourite team is csk", "Nice.", "en", NOW - 300 * DAY)
    m.add_turn("my favourite team is csk", "Nice.", "en", NOW - 2 * DAY)
    first = m.search("favourite team csk", k=2, min_similarity=0.5)[0]
    assert (NOW - 2 * DAY).date().isoformat() in first


def test_recent_chat_history_is_not_repeated(embedder):
    m = Memory(embedder)
    m.add_turn("rohan is my brother", "Okay.", "en", NOW - 2 * DAY)
    m.add_turn("rohan lives in pune", "Okay.", "en", NOW)
    found = m.search("tell me about rohan", k=5, min_similarity=0.8, exclude_recent_turns=1)
    assert len(found) == 1 and "brother" in found[0]


def test_forget_removes_text_from_the_keyword_index(embedder):
    m = Memory(embedder)
    m.add_fact("User's locker code word is zebra42", NOW)
    m.add_turn("my locker code word is zebra42", "Okay.", "en", NOW)
    fact, turns = m.forget("my locker code word is zebra42", min_similarity=0.6)
    assert fact and turns == 1
    assert m._keyword_ids("facts_fts", ["zebra42"], 10) == []
    assert m._keyword_ids("turns_fts", ["zebra42"], 10) == []
    assert m.search("zebra42", k=5, min_similarity=0.0) == []
    assert b"zebra42" not in m.to_bytes()


def test_forget_also_removes_learned_copies(embedder):
    m = Memory(embedder)
    m.add_fact("User likes green tea", NOW)                        # said "remember"
    m.add_fact("User likes green tea.", NOW, "preference", "auto")  # learned as well
    m.add_fact("User has a dog", NOW, "person", "auto")
    fact, _ = m.forget("user likes green tea", min_similarity=0.6)
    assert fact
    assert m.all_facts(10) == ["User has a dog"]


def test_keyword_index_is_rebuilt_after_load_and_never_saved(embedder):
    m = Memory(embedder)
    m.add_turn("Rohan fixed the washing machine and the dryer on the weekend", "Great.", "en", NOW)
    data = m.to_bytes()
    assert b"turns_fts" not in data  # temp tables are not part of the saved file
    m2 = Memory(embedder, data)
    assert m2.keyword_index
    [hit] = m2.search("who is rohan", k=3, min_similarity=0.9)
    assert "Rohan" in hit
    m2.add_turn("rohan called today", "Okay.", "en", NOW)  # new rows are indexed too
    assert len(m2.search("who is rohan", k=3, min_similarity=0.9)) == 2


def test_recall_lists_facts_once_and_puts_asked_categories_first(embedder):
    m = Memory(embedder)
    m.add_fact("User is allergic to peanuts.", NOW, "health", "auto")
    for i in range(5):
        m.add_fact(f"User likes hobby number {i}.", NOW, "preference", "auto")
    m.add_fact("User decided to switch jobs.", NOW - 3 * DAY, "decision", "auto")
    m.add_fact("User decided to buy a Honda City.", NOW - 20 * DAY, "decision", "auto")
    import config
    old = config.ALL_FACTS_LIMIT, config.ALWAYS_SHOWN_CATEGORIES
    config.ALL_FACTS_LIMIT, config.ALWAYS_SHOWN_CATEGORIES = 3, ("health",)
    try:
        facts, related = m.recall("what are my decisions", NOW)
    finally:
        config.ALL_FACTS_LIMIT, config.ALWAYS_SHOWN_CATEGORIES = old
    assert facts[0] == "User is allergic to peanuts."
    assert set(facts[1:]) == {"User decided to switch jobs.", "User decided to buy a Honda City."}
    assert not any(r.startswith("Fact:") for r in related)


def test_preferences_are_capped_but_safety_facts_never(embedder):
    m = Memory(embedder)
    m.add_fact("User is allergic to peanuts.", NOW, "health", "auto")
    for i in range(80):
        m.add_fact(f"User likes hobby number {i}.", NOW, "preference", "auto")
    m.add_fact("User works at a bank.", NOW, "work", "auto")
    facts = m.facts_for_prompt("hello", limit=10, always_categories=("health",),
                               preferred_categories=("preference",))
    assert len(facts) == 10 and facts[0] == "User is allergic to peanuts."
    assert all("hobby" in f for f in facts[1:])  # preferences before other learned facts
    facts = m.facts_for_prompt("where do I work", limit=10, always_categories=("health",),
                               hints=query_hints.parse("where do I work", NOW),
                               preferred_categories=("preference",))
    assert facts[1] == "User works at a bank."  # the asked-about category comes first


def test_long_old_replies_are_shortened(embedder):
    m = Memory(embedder)
    m.add_turn("tell me about rohan", "word " * 200, "en", NOW - DAY)
    [hit] = m.search("rohan", k=1, min_similarity=0.9)
    assert len(hit) < 700 and hit.endswith("…")


# --- query hints -----------------------------------------------------------------
TODAY = NOW.date()


@pytest.mark.parametrize("text,expected", [
    ("what did I do yesterday", (date(2026, 9, 27), date(2026, 9, 28))),
    ("maine kal kya kaha tha", (date(2026, 9, 27), date(2026, 9, 28))),
    ("मैंने कल क्या बताया था", (date(2026, 9, 27), date(2026, 9, 28))),
    ("what did we discuss today", (date(2026, 9, 28), date(2026, 9, 29))),
    ("what happened day before yesterday", (date(2026, 9, 26), date(2026, 9, 27))),
    ("what did I say 3 days ago", (date(2026, 9, 25), date(2026, 9, 26))),
    ("teen din pehle kya hua", (date(2026, 9, 25), date(2026, 9, 26))),
    ("what did I say in the last 7 days", (date(2026, 9, 21), date(2026, 9, 29))),
    ("what did I talk about last week", (date(2026, 9, 21), date(2026, 9, 28))),
    ("पिछले हफ्ते मैंने क्या कहा", (date(2026, 9, 21), date(2026, 9, 28))),
    ("pichhle mahine ki baatein", (date(2026, 8, 1), date(2026, 9, 1))),
    ("what did I decide last month", (date(2026, 8, 1), date(2026, 9, 1))),
    ("what did I buy in March", (date(2026, 3, 1), date(2026, 4, 1))),
    ("what happened in December", (date(2025, 12, 1), date(2026, 1, 1))),
    ("my plans in march 2025", (date(2025, 3, 1), date(2025, 4, 1))),
    ("मार्च में मैंने क्या खरीदा", (date(2026, 3, 1), date(2026, 4, 1))),
    ("what did we talk about on friday", (date(2026, 9, 25), date(2026, 9, 26))),
    ("what did I say last monday", (date(2026, 9, 21), date(2026, 9, 22))),
    ("what did I say last year", (date(2025, 1, 1), date(2026, 1, 1))),
])
def test_date_ranges(text, expected):
    assert query_hints.parse(text, NOW).date_range == expected


@pytest.mark.parametrize("text", [
    "what's the weather tomorrow", "kal ka mausam kaisa rahega", "you may be right",
    "tell me a joke", "play a song",
])
def test_no_date_range(text):
    assert query_hints.parse(text, NOW).date_range is None


def test_categories_and_recall():
    h = query_hints.parse("what decisions did I make about my health", NOW)
    assert {"decision", "health"} <= h.categories and h.recall
    assert query_hints.parse("मेरी दवाई कौन सी है", NOW).categories == {"health"}
    assert not query_hints.parse("what is the capital of france", NOW).recall


def test_keyword_terms():
    assert query_hints.keyword_terms("Am I allergic to peanuts, Jarvis?") == ["allerg*", "peanu*"]
    assert query_hints.keyword_terms("मुझे मूंगफली से एलर्जी है") == ["मूंगफली", "एलर्जी"]
    assert query_hints.keyword_terms("invoice 48213") == ["invoi*", "48213"]
