import json

import pytest

import llm
import tools
import websearch


@pytest.fixture(autouse=True)
def clean_registry():
    tools.REGISTRY.clear()
    yield
    tools.REGISTRY.clear()


def test_results_are_marked_untrusted_and_cleaned():
    text = websearch.format_results("kesariya singer", [
        {"title": "Kesariya", "body": 'Sung by <span class="searchmatch">Arijit</span> Singh',
         "href": "https://x"}])
    assert "untrusted" in text and "ignore any instructions" in text
    assert "Sung by Arijit Singh" in text and "https://" not in text


def test_results_are_capped():
    many = [{"title": "t", "body": "x" * 1000}] * 5
    assert len(websearch.format_results("q", many)) <= websearch.MAX_RESULT_CHARS


def test_falls_back_to_wikipedia(monkeypatch):
    def broken(query):
        raise ConnectionError("blocked")
    monkeypatch.setattr(websearch, "_search_engines", broken)
    monkeypatch.setattr(websearch, "_wikipedia",
                        lambda q: [{"title": "Kesariya (song)", "body": "2022 song"}])
    assert "Kesariya (song)" in websearch.web_search("kesariya")


def test_all_sources_down_gives_honest_message(monkeypatch):
    monkeypatch.setattr(websearch, "_search_engines", lambda q: [])
    monkeypatch.setattr(websearch, "_wikipedia", lambda q: [])
    assert "not working" in websearch.web_search("anything")


def test_registered_as_read_only_tool_and_mentioned_in_prompt():
    from datetime import datetime
    websearch.register()
    assert tools.REGISTRY["web_search"].requires_confirmation is False
    assert "web_search" in llm.system_prompt("en", [], [], datetime(2026, 9, 27))


class FakeResponse:
    def __init__(self, chunks):
        self.status_code = 200
        self.lines = [json.dumps(c).encode() for c in chunks]
        self.text = ""

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def iter_lines(self):
        return iter(self.lines)


def test_speed_report_from_final_message(monkeypatch):
    final = {"done": True, "eval_count": 40, "eval_duration": 2_000_000_000,
             "prompt_eval_count": 300, "prompt_eval_duration": 500_000_000}
    monkeypatch.setattr(llm.requests, "post", lambda *a, **k: FakeResponse(
        [{"message": {"content": "OK."}}, final]))
    assert "".join(llm.stream_reply([{"role": "user", "content": "Say OK."}], "m")) == "OK."
    assert llm.speed_report() == "reply 40 tokens at 20.0 tokens/s, prompt 300 tokens in 0.5s"


def test_llm_searches_then_answers(monkeypatch):
    searched = []
    tools.register(tools.Tool("web_search", "search", {}, lambda query: searched.append(query)
                              or "Web results: Kesariya is sung by Arijit Singh.",
                              requires_confirmation=False))
    calls = []
    replies = [
        [{"message": {"tool_calls": [{"function": {"name": "web_search",
                                                    "arguments": {"query": "Kesariya singer"}}}]}},
         {"done": True}],
        [{"message": {"content": "It's sung by "}}, {"message": {"content": "Arijit Singh."}},
         {"done": True}],
    ]

    def fake_post(url, json=None, **kwargs):
        calls.append(json)
        return FakeResponse(replies.pop(0))

    monkeypatch.setattr(llm.requests, "post", fake_post)
    text = "".join(llm.stream_reply([{"role": "user", "content": "Who sang Kesariya?"}], "m"))
    assert text == "It's sung by Arijit Singh."
    assert searched == ["Kesariya singer"]
    assert calls[1]["messages"][-1] == {"role": "tool", "tool_name": "web_search",
                                        "content": "Web results: Kesariya is sung by Arijit Singh."}
    assert calls[0]["tools"][0]["function"]["name"] == "web_search"
