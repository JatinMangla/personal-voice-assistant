"""The coder helper's Kaggle start-up logic (src/coder_main.py, src/coder_rerank.py) with fakes."""
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

import coder_main
import coder_proxy
import coder_rerank
import config
from memory import Memory

IST = ZoneInfo("Asia/Kolkata")


class FakeStore:
    def __init__(self, data=None, fail_load=False, fail_save=False):
        self.data, self.fail_load, self.fail_save, self.saves = data, fail_load, fail_save, []

    def load(self):
        if self.fail_load:
            raise RuntimeError("download failed")
        return self.data

    def save(self, plaintext, usage=None, **kwargs):
        if self.fail_save:
            return False
        self.data = plaintext
        self.saves.append(usage)
        return True


@pytest.fixture(autouse=True)
def ngrok_meters(monkeypatch):
    monkeypatch.setattr(config, "NGROK_IN_USE", True)
    monkeypatch.setattr(config, "SESSION_OVERHEAD_MIN", 2)


def recorder(store, state=None):
    clock = [1000.0]
    now = datetime(2026, 10, 9, 12, 0, tzinfo=IST)
    r = coder_main.UsageRecorder(store, state or coder_proxy.ProxyState(), clock=lambda: now,
                                 clock_seconds=lambda: clock[0])
    return r, clock


def saved_memory(store):
    return Memory(None, store.data)


def test_usage_is_added_on_top_of_the_latest_memory():
    base = Memory(None)
    base.add_usage("2026-10-09", 600)
    base.add_net("2026-10", 5, 1000)
    store = FakeStore(base.to_bytes())
    r, clock = recorder(store)
    clock[0] = 1000 + 300                     # 5 minutes of this run
    r.state.count_request()
    r.state.count(4000)
    meters = r.record()
    m = saved_memory(store)
    assert m.get_usage("2026-10-09") == 600 + 300 + 120      # earlier use + this run + start-up overhead
    assert m.get_net("2026-10") == (5 + 1, 1000 + 4000)
    assert store.saves[0]["days"]["2026-10-09"] == 1020       # the numbers the Start button reads
    assert {x.name for x in meters} == {"day", "week", "requests", "data"}


def test_second_record_adds_only_the_new_time_and_no_overhead_again():
    store = FakeStore(Memory(None).to_bytes())
    r, clock = recorder(store)
    clock[0] = 1300
    r.record()
    clock[0] = 1900
    r.record()
    assert saved_memory(store).get_usage("2026-10-09") == 120 + 900


def test_unreadable_memory_saves_nothing_and_keeps_the_time_for_later():
    store = FakeStore(Memory(None).to_bytes(), fail_load=True)
    r, clock = recorder(store)
    clock[0] = 1600
    assert r.record() is None and store.saves == []
    store.fail_load = False
    clock[0] = 1700
    assert r.record() is not None
    assert saved_memory(store).get_usage("2026-10-09") == 700 + 120   # nothing was lost


def test_failed_upload_keeps_traffic_for_the_next_try():
    store = FakeStore(Memory(None).to_bytes(), fail_save=True)
    r, clock = recorder(store)
    r.state.count_request()
    r.state.count(900)
    assert r.record() is None
    store.fail_save = False
    clock[0] = 1010
    r.record()
    assert saved_memory(store).get_net("2026-10") == (1, 900)


def test_first_ever_run_without_a_saved_memory_starts_one():
    store = FakeStore(None)
    r, clock = recorder(store)
    clock[0] = 1060
    assert r.record() is not None
    assert saved_memory(store).get_usage("2026-10-09") == 60 + 120


def test_stop_reasons():
    cfg = config
    assert coder_main.stop_reason(0, 10, None) is None
    assert "20 min" in coder_main.stop_reason(cfg.CODER_IDLE_MIN * 60, 10, None)
    assert "session cap" in coder_main.stop_reason(0, cfg.CODER_MAX_SESSION_MIN * 60, None)
    assert "day" in coder_main.stop_reason(0, 10, "day")
    assert coder_main.stop_reason(cfg.CODER_IDLE_MIN * 60 - 1, 10, None) is None


def test_modelfile_bakes_in_the_one_context_size():
    text = coder_main.modelfile("qwen3-coder:30b")
    assert text.startswith("FROM qwen3-coder:30b\n")
    assert f"PARAMETER num_ctx {config.CODER_CONTEXT_TOKENS}" in text


def test_ollama_env_has_kv_cache_only_with_flash_attention(monkeypatch):
    env = coder_main.ollama_env()
    assert env["OLLAMA_FLASH_ATTENTION"] == "1" and env["OLLAMA_KV_CACHE_TYPE"] == config.CODER_KV_CACHE_TYPE
    assert env["OLLAMA_NUM_PARALLEL"] == "1" and env["OLLAMA_KEEP_ALIVE"] == "-1"
    monkeypatch.setattr(config, "CODER_FLASH_ATTENTION", False)
    env = coder_main.ollama_env()
    assert env["OLLAMA_FLASH_ATTENTION"] == "0" and "OLLAMA_KV_CACHE_TYPE" not in env


def test_kernel_active_reads_kaggle_status_text():
    assert coder_main.kernel_active('jatinmangla123/jarvis has status "KernelWorkerStatus.RUNNING"')
    assert coder_main.kernel_active('status "QUEUED"')
    assert not coder_main.kernel_active('has status "KernelWorkerStatus.COMPLETE"')
    assert not coder_main.kernel_active("")


def test_speed_report():
    text = coder_main.speed_report({"load_duration": 4_000_000_000, "prompt_eval_count": 3000,
                                    "prompt_eval_duration": 3_000_000_000, "eval_count": 120,
                                    "eval_duration": 3_000_000_000})
    assert text == "load 4.0s, reading 3000 tok at 1000/s, writing 120 tok at 40/s"
    assert "n/a" in coder_main.speed_report({})


class Calls:
    def __init__(self):
        self.pulled, self.aliases, self.chats = [], [], 0


@pytest.fixture
def fake_ollama(monkeypatch):
    calls = Calls()
    monkeypatch.setattr(coder_main, "create_alias", lambda base: calls.aliases.append(base))

    def chat(path, body, timeout=900):
        calls.chats += 1
        assert body["model"] == config.CODER_ALIAS
        return {"eval_count": 5, "eval_duration": 1_000_000_000}
    monkeypatch.setattr(coder_main, "ollama_json", chat)
    return calls


def test_big_model_is_used_when_it_works(fake_ollama):
    def pull(model):
        fake_ollama.pulled.append(model)
        return True
    assert coder_main.load_coder_model(pull) == config.CODER_MODEL
    assert fake_ollama.pulled == [config.CODER_MODEL] and fake_ollama.aliases == [config.CODER_MODEL]
    assert fake_ollama.chats == 3        # load + answer, reading-speed test, writing-speed test


def test_smaller_model_is_used_when_the_big_one_does_not_download(fake_ollama):
    def pull(model):
        fake_ollama.pulled.append(model)
        return model != config.CODER_MODEL
    assert coder_main.load_coder_model(pull) == config.CODER_FALLBACK_MODEL
    assert fake_ollama.aliases == [config.CODER_FALLBACK_MODEL]


def test_smaller_model_is_used_when_the_big_one_runs_out_of_memory(monkeypatch, fake_ollama):
    state = {"first": True}

    def chat(path, body, timeout=900):
        if state["first"]:
            state["first"] = False
            raise OSError("llama runner process has terminated: out of memory")
        return {"eval_count": 5, "eval_duration": 1_000_000_000}
    monkeypatch.setattr(coder_main, "ollama_json", chat)
    assert coder_main.load_coder_model(lambda m: True) == config.CODER_FALLBACK_MODEL


def test_no_model_at_all_is_an_error(fake_ollama):
    with pytest.raises(RuntimeError, match="No coder model"):
        coder_main.load_coder_model(lambda m: False)


def test_rerank_prompt_format():
    text = coder_rerank.format_pair("where is the budget stop", "def over_stop(): ...")
    assert text.startswith("<Instruct>: ") and "<Query>: where is the budget stop" in text
    assert text.endswith("<Document>: def over_stop(): ...")
    assert "yes" in coder_rerank.PREFIX and "no" in coder_rerank.PREFIX
    assert coder_rerank.SUFFIX.endswith("</think>\n\n")


def test_reranker_that_cannot_load_gives_none_not_an_error(monkeypatch, capsys):
    monkeypatch.setattr(config, "CODER_RERANK_REPO", "does/not-exist")
    assert coder_rerank.load("cpu") is None
    assert "no reranker" in capsys.readouterr().out


# --- free-limit warning (owner's request: tell me before it stops) --------------------------------
def meters(day_fraction=0.0, requests_fraction=0.0):
    from budget import Meter
    return [Meter("day", config.DAILY_LIMIT_MIN * 60 * day_fraction, config.DAILY_LIMIT_MIN * 60),
            Meter("requests", config.NGROK_MONTHLY_REQUESTS * requests_fraction, config.NGROK_MONTHLY_REQUESTS)]


NOW = datetime(2026, 10, 9, 20, 0, tzinfo=IST)


def test_no_warning_while_there_is_plenty_left():
    assert coder_main.limit_warning(meters(day_fraction=0.2), NOW) is None
    assert coder_main.limit_warning([], NOW) is None


def test_warning_once_the_day_is_above_the_notify_level():
    note = coder_main.limit_warning(meters(day_fraction=0.92), NOW)
    assert note["meter"] == "day" and note["percent"] == 92
    assert "today's GPU time is at 92%" in note["text"] and "stops itself at 99%" in note["text"]
    assert "midnight" in note["text"].lower() and note["minutes_left"] < 25


def test_warning_when_little_time_is_left_even_below_the_notify_level():
    # 90% of the day used is under the 90% notify level in fraction terms, but only ~25 min remain
    note = coder_main.limit_warning(meters(day_fraction=0.895), NOW)
    assert note and note["minutes_left"] <= config.CODER_WARN_MIN_LEFT
    assert "min of Coder time left today" in note["text"]


def test_the_session_cap_never_promises_more_time_than_it_can_give():
    nearly_over = (config.CODER_MAX_SESSION_MIN - 4) * 60
    note = coder_main.limit_warning(meters(day_fraction=0.1), NOW, elapsed_s=nearly_over)
    assert note and note["minutes_left"] == 4            # the 3-hour cap, not the day's remainder


def test_the_tightest_meter_is_the_one_reported():
    note = coder_main.limit_warning(meters(day_fraction=0.5, requests_fraction=0.95), NOW)
    assert note["meter"] == "requests" and "free link requests" in note["text"]
    assert note["minutes_left"] is None                  # link meters have no minutes
    assert "October" in note["text"] or "November" in note["text"]


def test_the_watchdog_publishes_the_warning_and_logs_it_once(monkeypatch, capsys):
    class Server:
        should_exit = False

    class Recorder:
        def record(self):
            return meters(day_fraction=0.93)
    monkeypatch.setattr(coder_main.time, "sleep", lambda s: None)
    monkeypatch.setattr(config, "CODER_USAGE_EVERY_MIN", 0)
    state = coder_proxy.ProxyState()
    server, warning = Server(), {}
    ticks = [0]

    def over_limit_of(ms):
        ticks[0] += 1
        if ticks[0] >= 3:
            server.should_exit = True                    # end the loop after a few rounds
        return None
    coder_main.watchdog(state, Recorder(), server, coder_main.time.time(), over_limit_of, warning)
    assert warning["meter"] == "day" and "93%" in warning["text"]
    assert capsys.readouterr().out.count("free-limit warning") == 1   # said once, not every tick


def test_health_publishes_the_warning_and_the_client_passes_it_on():
    import httpx
    from fastapi.testclient import TestClient
    note = {"meter": "day", "label": "today's GPU time", "percent": 94, "minutes_left": 12,
            "resets": "Resets at midnight.", "text": "today's GPU time is at 94%; about 12 min left."}
    live = {}
    app = coder_proxy.create_app("k" * 43, "http://o.test", coder_proxy.ProxyState(),
                                 transport=httpx.MockTransport(lambda r: httpx.Response(200, json={})),
                                 warning=lambda: dict(live) or None)
    http = TestClient(app)
    assert http.get("/health", headers={"Authorization": "Bearer " + "k" * 43}).json()["limit_warning"] is None

    import sys
    sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent / "scripts"))
    from coder.client import CoderClient
    client = CoderClient("http://testserver", "k" * 43, http=http)
    assert client.online() and client.warning_line() == ""
    live.update(note)
    assert client.online()
    assert client.warning_line() == "\nFree limit: today's GPU time is at 94%; about 12 min left."


# --- experiment F: the Start button chooses the model -------------------------------------------
def test_the_30b_is_tried_first_by_default(monkeypatch):
    monkeypatch.setattr(config, "get_secret", lambda name, required=True: None)
    assert coder_main.wanted_models() == (config.CODER_MODEL, config.CODER_FALLBACK_MODEL)


def test_the_run_can_ask_for_the_14b_and_the_30b_becomes_the_fallback(monkeypatch):
    """Experiment F compares the two. config.CODER_MODEL is frozen at import from an environment a
    Start-button run does not have, so the choice has to come from the run's secrets bundle."""
    monkeypatch.setattr(config, "get_secret",
                        lambda name, required=True: config.CODER_FALLBACK_MODEL
                        if name == "PVA_CODER_MODEL" else None)
    assert coder_main.wanted_models() == (config.CODER_FALLBACK_MODEL, config.CODER_MODEL)


@pytest.mark.parametrize("value", ["llama3:70b", "../etc/passwd", "", "   ", "qwen3-coder:30b-x"])
def test_an_unknown_model_name_is_ignored_not_obeyed(monkeypatch, value):
    """A wrong name must not waste a start-up pulling something huge or nonexistent."""
    monkeypatch.setattr(config, "get_secret",
                        lambda name, required=True: value if name == "PVA_CODER_MODEL" else None)
    assert coder_main.wanted_models()[0] == config.CODER_MODEL


def test_a_broken_secrets_bundle_does_not_stop_the_run(monkeypatch):
    def boom(name, required=True):
        raise RuntimeError("no bundle here")
    monkeypatch.setattr(config, "get_secret", boom)
    assert coder_main.wanted_models() == (config.CODER_MODEL, config.CODER_FALLBACK_MODEL)


def test_the_model_is_passed_through_the_start_script_and_the_workflow():
    import yaml
    from pathlib import Path
    root = Path(__file__).resolve().parent.parent
    start = (root / "scripts" / "start_coder.py").read_text(encoding="utf-8")
    assert "PVA_CODER_MODEL" in start, "the start script must carry the model into the bundle"

    raw = (root / ".github" / "workflows" / "start-coder.yml").read_text(encoding="utf-8")
    data = yaml.safe_load(raw)
    trigger = next(data[k] for k in data if str(k) in ("on", "True"))
    model = trigger["workflow_dispatch"]["inputs"]["model"]
    assert model["default"] == config.CODER_MODEL, "the 30B must stay the default"
    assert set(model["options"]) == {config.CODER_MODEL, config.CODER_FALLBACK_MODEL}
    env = next(step["env"] for step in data["jobs"]["start"]["steps"] if "env" in step)
    assert "PVA_CODER_MODEL" in env
