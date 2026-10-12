"""scripts/coder_compare_methods.py: the ask_repo-versus-investigate judge.

It decides whether the agentic loop is worth its GPU time, so its grading and its
"is this gap bigger than the noise?" rule are tested like the benchmark's own.
"""
import importlib.util
import sys
from pathlib import Path

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))

spec = importlib.util.spec_from_file_location("coder_compare_methods",
                                              SCRIPTS / "coder_compare_methods.py")
compare = importlib.util.module_from_spec(spec)
spec.loader.exec_module(compare)


def test_both_methods_are_compared_and_named_as_the_coder_calls_them():
    from coder import service
    for method in compare.METHODS:
        assert hasattr(service.Coder, method), f"Coder has no {method}()"


def test_grading_uses_the_benchmarks_own_step_lists():
    """If this drifted from coder_bench.py the two sets of numbers could not be compared."""
    question, steps = compare.bench.TRACES[0]
    assert len(steps) == 10
    answer = "start_coder uploads to jarvis-secrets then does a kernels push of the notebook"
    got, missed = compare.grade(answer, steps)
    assert "start_coder.py" in got and "secrets upload" in got and "kernels push" in got
    assert "idle stop" in missed
    assert len(got) + len(missed) == len(steps)


def test_grading_is_case_insensitive_and_counts_each_step_once():
    _, steps = compare.bench.TRACES[0]
    got, _ = compare.grade("START_CODER START_CODER start_coder", steps)
    assert got.count("start_coder.py") == 1


def test_an_empty_answer_scores_nothing_rather_than_crashing():
    _, steps = compare.bench.TRACES[1]
    got, missed = compare.grade("", steps)
    assert got == [] and len(missed) == len(steps)


def test_the_recording_client_is_a_real_client_with_a_call_log():
    from coder.client import CoderClient
    assert issubclass(compare.Recording, CoderClient)
    client = compare.Recording("https://example.invalid", "k")
    assert client.calls == []
