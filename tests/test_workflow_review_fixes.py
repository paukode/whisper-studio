"""WS-D adversarial-review regression tests.

Each test pins a fix for a confirmed review finding so it can't silently
regress: the host-RCE sandbox escape, per-agent-model costing, parallel budget
overshoot, nested cap/spend inheritance, seq-ordered resume replay, and the
Date-guard prototype.constructor loophole.
"""

from __future__ import annotations

import asyncio
import os
import shutil
from collections import deque

import pytest

NODE = shutil.which("node") or "/usr/local/bin/node"
_HERE = os.path.dirname(os.path.dirname(__file__))
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(_HERE, "server", "workflows", "harness", "harness.mjs")
)
needs_harness = pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
    os.makedirs(tmp_path / "data", exist_ok=True)
    yield


def _run(coro):
    return asyncio.run(coro)


def _mk(source, **kw):
    from server.workflows.runtime import WorkflowRun

    return WorkflowRun("run-test", source, **kw)


async def _noop_agent(prompt, opts):
    return {"text": "ok", "usage": {}, "status": "completed"}


# ── HIGH: host-RCE sandbox escape must be blocked ──────────────────────────────
@needs_harness
def test_function_constructor_escape_blocked():
    # The classic vm escape: reach the host `Function` via a function's
    # constructor and compile a string into code. The harness runs node with
    # --disallow-code-generation-from-strings, so this raises EvalError rather
    # than returning the host `process`.
    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "const F = log.constructor;\n"  # any in-sandbox function → Function
        "const proc = F('return process')();\n"
        "return typeof proc;\n"
    )
    out = _run(_mk(src, agent_runner=_noop_agent).run())
    assert out["status"] == "failed"
    # EvalError text mentions code generation; never the host object.
    assert "process" not in str(out.get("result") or "")
    assert (
        "code generation" in out.get("error", "").lower()
        or "evalerror" in out.get("error", "").lower()
    )


# ── determinism: Date reached via prototype.constructor still guarded ──────────
@needs_harness
def test_date_prototype_constructor_guarded():
    # new Date() throws for determinism; the review flagged that Date could be
    # recovered via Date.prototype.constructor. guards.mjs redirects that back
    # to the guarded constructor, so the loophole also throws.
    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "const D = Date.prototype.constructor;\n"
        "return new D().getTime();\n"
    )
    out = _run(_mk(src, agent_runner=_noop_agent).run())
    assert out["status"] == "failed"
    assert "date" in out.get("error", "").lower()


# ── LOW: per-agent opts.model prices against that model, not the run default ────
def test_per_agent_model_costed_separately(monkeypatch):
    import server.costs.tracker as T

    seen = []

    def fake_cost(model_key, *a, **kw):
        seen.append(model_key)
        return 1.0

    monkeypatch.setattr(T, "estimate_cost", fake_cost)

    run = _mk("", agent_runner=_noop_agent, model_key="sonnet")
    # Default model call, then an opts.model override — the ledger must price
    # each against the model that actually ran.
    _run(run._handle_agent("1", {"prompt": "a", "opts": {}}))
    _run(run._handle_agent("2", {"prompt": "b", "opts": {"model": "gpt5.6"}}))
    assert seen == ["sonnet", "gpt5.6"]


# ── HIGH: parallel budget overshoot bounded by concurrency, not the agent cap ──
@needs_harness
def test_parallel_budget_overshoot_bounded(monkeypatch):
    # cost accrues on COMPLETION, so a burst of parallel dispatches all clear the
    # pre-dispatch budget check at cost≈0. The post-slot re-check must stop the
    # bleed well before the 1000-agent cap. With a $1 budget and $1/agent, only a
    # concurrency-window's worth may overshoot — nowhere near 200.
    import server.costs.tracker as T

    monkeypatch.setattr(T, "estimate_cost", lambda *a, **k: 1.0)

    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "const r = await parallel(Array.from({length:200}, (_,i)=>()=>"
        "agent('a'+i).then(()=>1).catch(()=>0)));\n"
        "return r.reduce((s,x)=>s+x,0);\n"  # how many actually ran
    )
    run = _mk(src, agent_runner=_noop_agent, budget_usd=1.0, model_key="sonnet")
    out = _run(run.run())
    assert out["status"] == "done"
    assert out["result"] < 50, f"overshoot unbounded: {out['result']} agents ran on a $1 budget"


# ── HIGH: nested run spend/agents/cap fold into the parent ledger ──────────────
def test_absorb_child_merges_ledger():
    parent = _mk("", model_key="sonnet")
    parent.agents_spawned = 2
    parent.cost_usd = 3.0
    parent.tokens_in = 10
    parent.tokens_out = 5
    parent.absorb_child(
        {
            "agents_spawned": 4,
            "cost_usd": 7.5,
            "tokens_in": 20,
            "tokens_out": 8,
            "cap_reached": True,
        }
    )
    assert parent.agents_spawned == 6
    assert parent.cost_usd == 10.5
    assert parent.tokens_in == 30 and parent.tokens_out == 13
    assert parent.cap_reached is True


# ── correctness: resume replay is FIFO by issue order (seq), not completion ─────
def test_resume_cache_ordered_by_seq(tmp_path):
    from server.workflows.journal import Journal, load_resume_cache

    # Two identical (prompt, opts) calls journal in COMPLETION order 2-then-1,
    # but the re-run issues them seq 1-then-2; replay must return them in seq
    # order or the two parallel results land swapped.
    j = Journal("resume-order")
    common = {
        "call_hash": "H",
        "status": "completed",
        "phase": "",
        "label": "",
        "output": None,
        "usage": {},
    }
    j.agent_call({"seq": 2, "text": "second", **common})
    j.agent_call({"seq": 1, "text": "first", **common})

    cache = load_resume_cache("resume-order")
    dq = cache["H"]
    assert [r["text"] for r in dq] == ["first", "second"]
