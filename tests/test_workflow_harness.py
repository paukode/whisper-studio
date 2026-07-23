"""WS-D slice 2: drive the Node harness over stdio JSON-RPC.

Spawns `node harness.mjs` and speaks the protocol directly: parse mode returns
meta without executing; run mode evaluates the script, proxies agent() calls
back to us, and returns the script's result; determinism guards reject
Math.random()/Date.now().
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess

import pytest

NODE = shutil.which("node") or "/usr/local/bin/node"
HARNESS = os.path.join(
    os.path.dirname(os.path.dirname(__file__)),
    "server",
    "workflows",
    "harness",
    "harness.mjs",
)

pytestmark = pytest.mark.skipif(
    not (os.path.exists(NODE) and os.path.exists(HARNESS)), reason="node or harness missing"
)


def _drive(source, *, mode="run", args=None, agent_responder=None, timeout=15):
    """Run the harness for one script; return (events, final) where final is the
    done/fatal message. agent_responder(params, seq) -> result dict."""
    proc = subprocess.Popen(
        [NODE, HARNESS],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        bufsize=1,
    )
    events = []
    final = None
    seq = 0
    try:
        proc.stdin.write(
            json.dumps(
                {"method": "start", "params": {"mode": mode, "source": source, "args": args}}
            )
            + "\n"
        )
        proc.stdin.flush()
        import time

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            line = proc.stdout.readline()
            if not line:
                break
            msg = json.loads(line)
            if msg.get("id") is not None and "method" in msg:
                # A request from the harness (agent/workflow/budget_spent).
                result = {}
                if msg["method"] == "agent" and agent_responder:
                    result = agent_responder(msg["params"], seq)
                    seq += 1
                elif msg["method"] == "budget_spent":
                    result = {"spent": 0}
                proc.stdin.write(json.dumps({"id": msg["id"], "result": result}) + "\n")
                proc.stdin.flush()
            elif "method" in msg:
                events.append(msg)
                if msg["method"] in ("done", "fatal"):
                    final = msg
                    break
    finally:
        try:
            proc.stdin.close()
        except Exception:
            pass
        proc.wait(timeout=5)
    return events, final


def test_parse_mode_extracts_meta_without_executing():
    src = (
        "export const meta = { name: 'demo', description: 'does a thing', "
        "phases: [{title:'explore'}, 'verify'] }\n"
        "throw new Error('this body must NOT run in parse mode')\n"
    )
    events, final = _drive(src, mode="parse")
    meta_ev = next(e for e in events if e["method"] == "meta")
    meta = meta_ev["params"]["meta"]
    assert meta["name"] == "demo"
    assert meta["phases"] == ["explore", "verify"]


def test_parse_rejects_impure_meta():
    src = "export const meta = { name: 'x', description: 'y', phases: [sideEffect()] }\n"
    events, final = _drive(src, mode="parse")
    assert final and final["method"] == "fatal"


def test_run_mode_proxies_agents_and_returns_result():
    src = (
        "export const meta = { name: 'r', description: 'run', phases: ['work'] }\n"
        "phase('work')\n"
        "const a = await agent('do one')\n"
        "const b = await parallel([() => agent('p1'), () => agent('p2')])\n"
        "return { a: a.text, b: b.map(x => x.text) }\n"
    )

    def responder(params, seq):
        return {"text": f"ans:{params['prompt']}", "output": None, "status": "completed"}

    events, final = _drive(src, agent_responder=responder)
    assert final and final["method"] == "done", final
    result = final["params"]["result"]
    assert result["a"] == "ans:do one"
    assert set(result["b"]) == {"ans:p1", "ans:p2"}
    assert any(e["method"] == "phase" and e["params"]["name"] == "work" for e in events)


def test_pipeline_runs_stages_per_item():
    src = (
        "export const meta = { name: 'p', description: 'pipe', phases: ['a','b'] }\n"
        "const out = await pipeline([1,2], "
        "  (item) => agent(`stage1 ${item}`), "
        "  (prev, item) => agent(`stage2 ${item} after ${prev.text}`))\n"
        "return out.map(o => o.text)\n"
    )

    def responder(params, seq):
        return {"text": params["prompt"], "status": "completed"}

    events, final = _drive(src, agent_responder=responder)
    assert final["method"] == "done"
    texts = final["params"]["result"]
    assert any("stage2 1 after stage1 1" in t for t in texts)
    assert any("stage2 2 after stage1 2" in t for t in texts)


def test_determinism_guard_blocks_math_random():
    src = (
        "export const meta = { name: 'd', description: 'det', phases: [] }\nreturn Math.random()\n"
    )
    events, final = _drive(src)
    assert final["method"] == "fatal"
    assert "DeterminismError" in final["params"]["error"]


def test_determinism_guard_blocks_date_now():
    src = "export const meta = { name: 'd', description: 'det', phases: [] }\nreturn Date.now()\n"
    events, final = _drive(src)
    assert final["method"] == "fatal" and "DeterminismError" in final["params"]["error"]


def test_date_with_args_allowed():
    src = (
        "export const meta = { name: 'd', description: 'det', phases: [] }\n"
        "return new Date('2026-01-01').getFullYear()\n"
    )
    events, final = _drive(src)
    assert final["method"] == "done" and final["params"]["result"] == 2026


def test_no_fs_or_process_globals():
    src = (
        "export const meta = { name: 'd', description: 'det', phases: [] }\n"
        "return typeof process + ',' + typeof require + ',' + typeof fetch\n"
    )
    events, final = _drive(src)
    assert final["method"] == "done"
    assert final["params"]["result"] == "undefined,undefined,undefined"


def test_parallel_thunk_error_becomes_null():
    src = (
        "export const meta = { name: 'x', description: 'y', phases: [] }\n"
        "const r = await parallel([() => agent('ok'), () => { throw new Error('boom') }])\n"
        "return [r[0].text, r[1]]\n"
    )

    def responder(params, seq):
        return {"text": "OK", "status": "completed"}

    events, final = _drive(src, agent_responder=responder)
    assert final["method"] == "done"
    assert final["params"]["result"] == ["OK", None]
