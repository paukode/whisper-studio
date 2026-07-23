"""WS-J slice 2: failing-log diagnosis + autofix workflow generation.

The load-bearing test is that the script J generates PARSES through D's real
Node harness — J and D share a contract, so a drift breaks the build here.
"""

from __future__ import annotations

import os
import shutil

import pytest

from server.ci import autofix, diagnose

NODE = shutil.which("node") or "/usr/local/bin/node"
_HERE = os.path.dirname(os.path.dirname(__file__))
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(_HERE, "server", "workflows", "harness", "harness.mjs")
)


# ── diagnose ─────────────────────────────────────────────────────────────
def test_diagnose_parses_findings(monkeypatch):
    reply = (
        'Here you go: {"findings":[{"check":"Backend","category":"LINT",'
        '"summary":"ruff E501","error_excerpt":"line too long","suspect_files":["a.py"],'
        '"suggested_fix":"wrap the line"}]} done'
    )
    monkeypatch.setattr(diagnose, "_one_shot", lambda *a, **k: reply)
    out = diagnose.diagnose({"workflow": "CI"}, "log", failed_job_names=["Backend"])
    assert len(out) == 1
    f = out[0]
    assert f["check"] == "Backend"
    assert f["category"] == "lint"  # normalized lowercase, in the allowed set
    assert f["suspect_files"] == ["a.py"]


def test_diagnose_unknown_category_becomes_other(monkeypatch):
    monkeypatch.setattr(
        diagnose,
        "_one_shot",
        lambda *a,
        **k: '{"findings":[{"check":"X","category":"kaboom","summary":"s","suggested_fix":"f"}]}',
    )
    out = diagnose.diagnose({}, "log")
    assert out[0]["category"] == "other"


def test_diagnose_empty_on_bad_reply(monkeypatch):
    monkeypatch.setattr(diagnose, "_one_shot", lambda *a, **k: "no json here at all")
    assert diagnose.diagnose({}, "log") == []


def test_diagnose_empty_when_model_unavailable(monkeypatch):
    monkeypatch.setattr(diagnose, "_one_shot", lambda *a, **k: None)
    assert diagnose.diagnose({}, "log") == []


# ── autofix script generation ────────────────────────────────────────────
_FINDINGS = [
    {
        "check": "Backend",
        "category": "test",
        "summary": "test_foo fails",
        "error_excerpt": "AssertionError: 1 != 2",
        "suspect_files": ["server/foo.py"],
        "suggested_fix": "return 2 from foo()",
    },
    {
        "check": "Frontend `tsc`",  # backticks in text must not break the script
        "category": "type",
        "summary": "TS2345 on Bar.tsx",
        "error_excerpt": "Type 'x' is not assignable",
        "suspect_files": ["src/Bar.tsx"],
        "suggested_fix": "widen the ${prop} type",  # a literal ${...} must stay inert
    },
]


def test_build_script_has_two_phases_and_embeds_findings():
    src = autofix.build_autofix_script("feat/x", _FINDINGS)
    assert "export const meta" in src
    assert "phase('Fix')" in src and "phase('Verify')" in src
    assert "verify_change" in src
    # findings are embedded as re-parsed JSON, not interpolated into the template
    assert "JSON.parse(" in src


@pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")
def test_generated_script_parses_through_d_harness(tmp_path, monkeypatch):
    # The whole point of routing autofix through D: the script J emits must be a
    # valid D workflow. Parse it with the real Node harness meta-extractor.
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    from server.workflows.runtime import parse_workflow

    src = autofix.build_autofix_script("feat/x", _FINDINGS)
    meta = parse_workflow(src)  # raises ValueError if the script is malformed
    assert meta["name"] == "ci-autofix"
    # D normalizes phase objects to their title strings.
    assert meta["phases"] == ["Fix", "Verify"]


def test_generated_script_survives_adversarial_text(tmp_path, monkeypatch):
    if not HARNESS_OK:
        pytest.skip("node/harness missing")
    monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path))
    from server.workflows.runtime import parse_workflow

    nasty = [
        {
            "check": "`);process.exit(1);//",
            "category": "other",
            "summary": 'quote " and backslash \\ and newline \n',
            "error_excerpt": "${globalThis}",
            "suspect_files": ["a`b.py"],
            "suggested_fix": "'; return 42; '",
        }
    ]
    meta = parse_workflow(autofix.build_autofix_script("b", nasty))
    assert meta["name"] == "ci-autofix"


# ── plan_autofix wiring ──────────────────────────────────────────────────
def test_plan_autofix_builds_script(monkeypatch):
    run = {
        "run_id": 7,
        "branch": "feat/x",
        "url": "u",
        "jobs": [{"name": "Backend", "conclusion": "failure"}],
    }
    monkeypatch.setattr(autofix.provider, "failing_log", lambda *a, **k: "boom")
    monkeypatch.setattr(
        autofix.diagnose,
        "diagnose",
        lambda *a, **k: [
            {"check": "Backend", "category": "test", "summary": "s", "suggested_fix": "f"}
        ],
    )
    plan = autofix.plan_autofix(run, "/repo")
    assert plan["run_id"] == 7 and plan["branch"] == "feat/x"
    assert plan["failed_jobs"] == ["Backend"]
    assert plan["script"] and "ci-autofix" in plan["script"]


def test_plan_autofix_no_findings(monkeypatch):
    run = {"run_id": 7, "branch": "b", "jobs": []}
    monkeypatch.setattr(autofix.provider, "failing_log", lambda *a, **k: "")
    monkeypatch.setattr(autofix.diagnose, "diagnose", lambda *a, **k: [])
    plan = autofix.plan_autofix(run, "/repo")
    assert plan["script"] is None
    assert "nothing to autofix" in plan["summary"].lower()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
