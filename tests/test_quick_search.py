"""Songs/videos/prices are searched by rules before the model answers (quick_search.py)."""
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import quick_search
import tools
import websearch
from memory import Memory
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))


@pytest.mark.parametrize("text,tool,words,play", [
    ("play Arijit Singh's latest song", "find_video", "Arijit Singh's latest song", True),
    ("Hey Jarvis, play Kesariya", "find_video", "Kesariya", True),
    ("Kesariya bajao", "find_video", "Kesariya", True),
    ("arijit singh ke gaane sunao", "find_video", "arijit singh ke gaane", True),
    ("यूट्यूब पर भजन चलाओ", "find_video", "भजन", True),
    ("suggest some romantic songs", "find_video", "romantic songs", False),
    ("मुझे कुछ अच्छे गाने सुझाओ", "find_video", "अच्छे गाने", False),
    ("YouTube link of Kesariya", "find_video", "Kesariya", False),
    ("play the trailer of Pushpa 2", "find_video", "the trailer of Pushpa 2", True),
    ("price of boAt Airdopes 141", "find_prices", "boAt Airdopes 141", False),
    ("What is the cheapest price of Samsung Galaxy S24 in India?", "find_prices", "Samsung Galaxy S24", False),
    ("iPhone 16 kitne ka hai", "find_prices", "iPhone 16", False),
    ("आईफोन 16 की कीमत क्या है", "find_prices", "आईफोन 16", False),
    ("कीबोर्ड की कीमत बताओ", "find_prices", "कीबोर्ड", False),  # "की" inside a word stays
])
def test_detects_song_video_and_price_requests(text, tool, words, play):
    found = quick_search.detect(text)
    assert found is not None
    key = "query" if tool == "find_video" else "product"
    assert (found.tool, found.arguments[key], found.play) == (tool, words, play)


@pytest.mark.parametrize("text,words", [
    ("suggest a good laptop for coding", "a good laptop for coding"),
    ("recommend some restaurants in Noida", "restaurants in Noida"),
    ("कोई अच्छी किताब सुझाओ", "अच्छी किताब"),
])
def test_other_suggestions_search_the_web_first(text, words):
    found = quick_search.detect(text)
    assert (found.tool, found.arguments["query"], found.play) == ("web_search", words, False)


def test_latest_is_noticed():
    assert quick_search.detect("play Arijit Singh's latest song").arguments["latest"] is True
    assert quick_search.detect("naye punjabi gaane chalao").arguments["latest"] is True
    assert quick_search.detect("play Kesariya").arguments["latest"] is False


@pytest.mark.parametrize("text", [
    "let's play a game", "who sang Kesariya", "what is the weather",
    "how much did I pay for electricity bills", "how much GPU time is left",
    "remind me to pay the rent", "", "play",
])
def test_everything_else_is_left_to_the_model(text):
    assert quick_search.detect(text) is None


# --- in a turn -------------------------------------------------------------------------
@pytest.fixture
def turn(embedder, monkeypatch):
    tools.REGISTRY.clear()
    websearch._links.clear()
    websearch.register()
    sent = []

    def fake_videos(query, latest):
        return [{"title": "Kesariya", "content": "https://www.youtube.com/watch?v=BddP6PYo2gs",
                 "publisher": "YouTube", "duration": "4:28", "uploader": "Sony Music"}]
    monkeypatch.setattr(websearch, "_videos", fake_videos)

    def fake_reply(messages, model=None):
        sent.append(messages)
        return iter(["Playing Kesariya on YouTube."])
    monkeypatch.setattr(app_module.llm, "stream_reply", fake_reply)
    monkeypatch.setattr(app_module.learn, "learn", lambda m, t, n: [])

    class STT:
        def transcribe(self, audio, quiet=False):
            return "play Kesariya", "en"

    class TTS:
        def synthesize(self, text, lang):
            return b"ID3"

    class Store:
        last_saved = None

    assistant = app_module.Assistant(STT(), TTS(), Memory(embedder), Store(),
                                     SessionTimer(datetime.now(IST), 600))
    client = TestClient(app_module.create_app(assistant))
    yield assistant, client, sent
    tools.REGISTRY.clear()
    websearch._links.clear()


def _events(r):
    return [json.loads(line) for line in r.text.splitlines() if line.strip()]


def test_play_shows_the_card_first_and_the_app_opens_it(turn):
    assistant, client, sent = turn
    token = assistant.devices.mint("Phone", datetime.now(IST))
    evs = _events(client.post("/api/turn", content=b"audio",
                              headers={"X-Jarvis-App": "1", "Authorization": f"Bearer {token}"}))
    kinds = [e["type"] for e in evs]
    assert kinds.index("links") < kinds.index("sentence")  # cards while Jarvis speaks
    link = next(e for e in evs if e["type"] == "links")["links"][0]
    assert link["video_id"] == "BddP6PYo2gs" and link["play"] is True
    tool_msgs = [m for m in sent[0] if m["role"] == "tool"]
    assert "Videos found" in tool_msgs[0]["content"] and "phone app opens" in tool_msgs[0]["content"]
    assert sent[0][-2]["tool_calls"][0]["function"]["name"] == "find_video"
    assert kinds.count("links") == 1  # not shown twice


def test_page_gets_the_card_but_no_automatic_play(turn):
    _, client, sent = turn
    evs = _events(client.post("/api/turn", content=b"audio"))
    link = next(e for e in evs if e["type"] == "links")["links"][0]
    assert "play" not in link
    assert "tap Play" in [m for m in sent[0] if m["role"] == "tool"][0]["content"]


def test_youtube_results_from_web_search_become_video_cards(monkeypatch):
    websearch._links.clear()
    monkeypatch.setattr(websearch, "_search_engines", lambda q, max_results=None: [
        {"title": "Kesariya - YouTube", "href": "https://www.youtube.com/watch?v=BddP6PYo2gs", "body": "x"}])
    websearch.web_search("Kesariya YouTube")
    link = websearch.take_links(enrich=False)[0]
    assert link["kind"] == "video" and link["video_id"] == "BddP6PYo2gs"
