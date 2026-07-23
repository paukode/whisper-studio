"""WS-E cron verification: a run that doesn't satisfy its prompt is extended
(up to MAX_CONTINUATIONS) and, if still unmet, pushed as failed with an
[UNVERIFIED] prefix. The report content is always preserved.

Reuses the fake-Bedrock cron harness from test_cron_tools.
"""

from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(__file__))
from test_cron_tools import _FakeBedrock, _run_cron_job  # noqa: E402

_JOB = {
    "id": "job-verify",
    "name": "verify-job",
    "prompt": "produce the weekly report",
    "session_id": "sess-v",
    "schedule": {"type": "interval", "seconds": 1800},
    "enabled": True,
}


async def _noop_route(name, tool_input, **kwargs):
    return ("ok", [])


def _run(monkeypatch, verdict_seq):
    """Drive a cron job with cron_verify.verify returning each verdict in
    ``verdict_seq`` in turn (last repeats)."""
    import server.goals.cron_verify as CV

    calls = {"n": 0}

    def fake_verify(prompt, messages, notifications=None):
        i = min(calls["n"], len(verdict_seq) - 1)
        calls["n"] += 1
        return verdict_seq[i]

    monkeypatch.setattr(CV, "verify", fake_verify)
    recorded: dict = {}
    # verify() is stubbed above, so opting into cron_verify is network-safe
    # (the harness defaults it off to keep the real evaluator out of tests).
    _run_cron_job(monkeypatch, dict(_JOB), _FakeBedrock(), _noop_route, recorded, cron_verify=True)
    return recorded, calls


def test_achieved_pushes_ok(monkeypatch):
    from server.goals import Verdict

    recorded, _ = _run(monkeypatch, [Verdict("achieved", "looks complete", 0.9)])
    assert recorded["status"] == "ok"
    assert "[UNVERIFIED]" not in recorded["text"]


def test_unmet_extends_then_marks_unverified(monkeypatch):
    from server.goals import Verdict

    # Always not_achieved: the run extends twice (MAX_CONTINUATIONS) then fails.
    recorded, calls = _run(monkeypatch, [Verdict("not_achieved", "missing the summary", 0.5)])
    assert recorded["status"] == "failed"
    assert recorded["text"].startswith("[UNVERIFIED]")
    assert "missing the summary" in recorded["text"]
    # Verified at least three times (initial + 2 continuations).
    assert calls["n"] >= 3


def test_unmet_then_met_pushes_ok(monkeypatch):
    from server.goals import Verdict

    # First check fails (one continuation), second passes.
    recorded, _ = _run(
        monkeypatch,
        [Verdict("not_achieved", "keep going", 0.5), Verdict("achieved", "done now", 0.9)],
    )
    assert recorded["status"] == "ok"
    assert "[UNVERIFIED]" not in recorded["text"]


def test_verify_sees_notify_user_content(monkeypatch):
    # A run that delivers its report via notify_user must not be judged as
    # missing its deliverable: the notifications are folded into what the
    # evaluator reads.
    import server.goals.cron_verify as CV
    import server.goals.evaluator as EV

    seen = {}

    def fake_evaluate(goal, messages, *, provider="anthropic"):
        from server.goals import Verdict
        from server.goals.tail import render_tail

        seen["tail"] = render_tail(messages)
        return Verdict("achieved", "report delivered", 0.9)

    monkeypatch.setattr(EV, "evaluate", fake_evaluate)
    v = CV.verify("send the weekly report", [], ["Here is the weekly report: all systems go"])
    assert v.is_achieved
    assert "all systems go" in seen["tail"]


def test_final_round_is_still_verified(monkeypatch):
    # A run that finishes on its LAST allowed round must still be verified —
    # only the continuation is round-gated. With the round cap at 2 the fake
    # harness's end_turn (round 2) lands exactly on the final round.
    from server.goals import Verdict

    monkeypatch.setattr("server.cron_run.CRON_MAX_ROUNDS_DEFAULT", 2)
    recorded, calls = _run(monkeypatch, [Verdict("not_achieved", "missing the summary", 0.6)])
    assert calls["n"] >= 1  # verify DID run on the final round
    assert recorded["status"] == "failed"
    assert recorded["text"].startswith("[UNVERIFIED]")


def test_verify_helper_never_fails_the_run(monkeypatch):
    # cron_verify.verify must return achieved (not raise) if the evaluator dies.
    import server.goals.cron_verify as CV
    import server.goals.evaluator as EV

    def boom(*a, **k):
        raise RuntimeError("evaluator down")

    monkeypatch.setattr(EV, "evaluate", boom)
    v = CV.verify("prompt", [])
    assert v.is_achieved


@pytest.mark.parametrize("flag_on", [True, False])
def test_flag_off_is_legacy_behavior(monkeypatch, flag_on):
    import server.infrastructure.feature_flags as FF
    from server.goals import Verdict

    real = FF.is_enabled
    monkeypatch.setattr(FF, "is_enabled", lambda n: flag_on if n == "cron_verify" else real(n))
    # With the flag off, verify must never be consulted and status stays ok.
    import server.goals.cron_verify as CV

    monkeypatch.setattr(
        CV, "verify", lambda p, m, n=None: Verdict("not_achieved", "would fail", 0.9)
    )
    recorded: dict = {}
    _run_cron_job(monkeypatch, dict(_JOB), _FakeBedrock(), _noop_route, recorded)
    if flag_on:
        assert recorded["status"] == "failed"
    else:
        assert recorded["status"] == "ok"
