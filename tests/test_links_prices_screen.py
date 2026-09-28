"""Link preview cards, YouTube play links, price search, screen sharing, short answers."""
import base64
import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

import app as app_module
import llm
import pages
import tools
import websearch
from memory import Memory
from timer import SessionTimer

IST = timezone(timedelta(hours=5, minutes=30))

PRODUCT_PAGE = """<html><head><title>Boat Airdopes 141 - Buy</title>
<meta property="og:title" content="boAt Airdopes 141">
<meta property="og:image" content="/img/airdopes.jpg">
<meta property="og:site_name" content="ShopX">
<script type="application/ld+json">
{"@type": "Product", "name": "boAt Airdopes 141",
 "offers": {"@type": "Offer", "price": "1299.00", "priceCurrency": "INR"}}
</script></head><body>Hello</body></html>"""


@pytest.fixture(autouse=True)
def clean():
    tools.REGISTRY.clear()
    websearch._links.clear()
    yield
    tools.REGISTRY.clear()
    websearch._links.clear()


# --- reading pages ---------------------------------------------------------------------
def test_preview_reads_og_tags_and_json_ld_price(monkeypatch):
    monkeypatch.setattr(pages, "fetch_html", lambda url: PRODUCT_PAGE)
    info = pages.preview("https://shopx.example/airdopes")
    assert info["title"] == "boAt Airdopes 141"
    assert info["image"] == "https://shopx.example/img/airdopes.jpg"
    assert info["site"] == "ShopX"
    assert info["price"] == 1299.0 and info["currency"] == "INR"
    assert pages.format_price(info["price"], info["currency"]) == "₹1,299"


@pytest.mark.parametrize("text, price", [
    ("Buy now at ₹1,299 only", 1299.0), ("Rs. 45,999.00 with offers", 45999.0),
    ("INR 799", 799.0), ("no price here", None)])
def test_price_from_text(text, price):
    assert pages.price_from_text(text) == price


@pytest.mark.parametrize("url", ["http://127.0.0.1/admin", "http://169.254.169.254/latest",
                                 "http://10.0.0.5/", "file:///etc/passwd", "ftp://x.com/a"])
def test_private_and_non_web_addresses_are_never_fetched(url):
    assert pages.is_public_url(url) is False
    assert pages.fetch_html(url) is None


@pytest.mark.parametrize("url, vid", [
    ("https://www.youtube.com/watch?v=BddP6PYo2gs", "BddP6PYo2gs"),
    ("https://youtu.be/BddP6PYo2gs?t=3", "BddP6PYo2gs"),
    ("https://www.youtube.com/shorts/BddP6PYo2gs", "BddP6PYo2gs"),
    ("https://vimeo.com/123", None)])
def test_youtube_id(url, vid):
    assert pages.youtube_id(url) == vid


# --- videos and prices -------------------------------------------------------------------
def test_find_video_makes_play_card_with_thumbnail(monkeypatch):
    seen = {}

    def fake_videos(query, latest):
        seen["latest"] = latest
        return [{"title": "Arijit Singh - New Song", "content": "https://www.youtube.com/watch?v=BddP6PYo2gs",
                 "publisher": "YouTube", "duration": "4:11", "uploader": "T-Series",
                 "published": "2026-09-20T10:00:00"}]
    monkeypatch.setattr(websearch, "_videos", fake_videos)
    text = websearch.find_video("Arijit Singh new song", latest=True)
    assert seen["latest"] is True and "published 2026-09-20" in text
    link = websearch.take_links()[0]
    assert link["kind"] == "video" and link["video_id"] == "BddP6PYo2gs"
    assert link["image"] == "https://i.ytimg.com/vi/BddP6PYo2gs/hqdefault.jpg"


def test_find_prices_cheapest_first_with_cards(monkeypatch):
    results = [
        {"title": "Airdopes 141 at ShopA", "href": "https://shopa.example/p", "body": "Deal ₹1,499"},
        {"title": "Airdopes 141 ShopB", "href": "https://shopb.example/p", "body": "Buy now"},
        {"title": "Review blog", "href": "https://blog.example/r", "body": "Great buds"},
    ]
    monkeypatch.setattr(websearch, "_search_engines", lambda q, max_results=None: results)
    monkeypatch.setattr(pages, "previews", lambda urls: [
        {}, {"price": 1299.0, "currency": "INR", "site": "ShopB", "image": "https://shopb.example/i.jpg"}, {}])
    text = websearch.find_prices("boAt Airdopes 141")
    assert text.index("ShopB: ₹1,299") < text.index("shopa.example: ₹1,499")
    links = websearch.take_links()
    assert [l["price"] for l in links] == ["₹1,299", "₹1,499"]
    assert links[0]["kind"] == "product" and links[0]["image"] == "https://shopb.example/i.jpg"


def test_cards_get_thumbnails_from_page_previews(monkeypatch):
    websearch._keep_link("ISRO news", "https://news.example/isro")
    monkeypatch.setattr(pages, "previews", lambda urls: [
        {"image": "https://news.example/pic.jpg", "site": "News", "description": "Launch today"}])
    link = websearch.take_links()[0]
    assert link["image"] == "https://news.example/pic.jpg" and link["snippet"] == "Launch today"


def test_all_three_internet_tools_registered():
    websearch.register()
    assert {"web_search", "find_video", "find_prices"} <= set(tools.REGISTRY)
    assert "latest" in tools.REGISTRY["find_video"].parameters["properties"]


# --- short answers ------------------------------------------------------------------------
def test_prompt_asks_for_few_words():
    prompt = llm.system_prompt("en", [], [], datetime(2026, 9, 28))
    assert "as few words as possible" in prompt and "under 20 words" in prompt


# --- screen sharing -------------------------------------------------------------------------
def test_screen_snapshot_goes_to_the_model_once(embedder, monkeypatch):
    sent = []

    def fake_reply(messages, model=None):
        sent.append(messages)
        return iter(["That's a Python error."])
    monkeypatch.setattr(app_module.llm, "stream_reply", fake_reply)
    monkeypatch.setattr(app_module.learn, "learn", lambda m, t, n: [])

    class STT:
        def transcribe(self, audio, quiet=False):
            return "what is this error on my screen", "en"

    class TTS:
        pipelines = {"en": None}

        def synthesize(self, text, lang):
            return b"RIFF"

    class Store:
        last_saved = None

    memory = Memory(embedder)
    assistant = app_module.Assistant(STT(), TTS(), memory, Store(),
                                     SessionTimer(datetime.now(IST), 600))
    client = TestClient(app_module.create_app(assistant))
    jpeg = b"\xff\xd8\xff\xe0" + b"x" * 100
    assert client.post("/api/screen", content=jpeg).json() == {"ok": True}
    client.post("/api/turn", content=b"audio")
    client.post("/api/turn", content=b"audio")  # next question: no screen any more

    first, second = sent[0], sent[1]
    assert first[-1]["images"] == [base64.b64encode(jpeg).decode()]
    assert "sharing their screen" in first[0]["content"]
    assert "images" not in second[-1]
    assert b"xxxx" not in memory.to_bytes()  # the screenshot is never stored

    assert client.post("/api/screen", content=b"not an image").status_code == 400
