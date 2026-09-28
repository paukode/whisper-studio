"""The remaining direct Bedrock callers honour Local mode: /doctor neither pings
Bedrock nor reports a correctly configured Local install as broken, the
opt-in buddy fact builds no Bedrock client, and CI autofix says why it cannot
diagnose instead of reporting "no actionable failure". Hybrid and Cloud keep
all three."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from server import buddy, doctor


@pytest.fixture
def set_mode(monkeypatch):
    from server.infrastructure import config as cfg_mod

    real = cfg_mod.load_config()

    def _set(mode: str, **extra) -> None:
        cfg = {**real, "model_mode": mode, **extra}
        monkeypatch.setattr(cfg_mod, "load_config", lambda *a, **k: cfg)

    return _set


@pytest.fixture
def boto_clients(monkeypatch):
    built: list[str] = []

    class _Client:
        def invoke_model(self, **_kw):
            raise RuntimeError("stub: no network in tests")

    def client(service, **_kw):
        built.append(service)
        return _Client()

    monkeypatch.setattr("boto3.client", client)
    return built


_AWS_CHECKS = {"AWS credentials", "Bedrock connectivity"}


@pytest.mark.parametrize("mode", ["local", "hybrid", "cloud"])
def test_doctor_pings_bedrock_only_outside_local_mode(set_mode, boto_clients, mode):
    set_mode(mode)
    report = asyncio.run(doctor.doctor())
    aws_rows = [r for r in report["checks"] if r["check"] in _AWS_CHECKS]
    assert {r["check"] for r in aws_rows} == _AWS_CHECKS  # the rows are always listed
    if mode == "local":
        assert boto_clients == []
        assert all(r["status"] == "ok" and "Local mode" in r["detail"] for r in aws_rows)
    else:
        assert "bedrock-runtime" in boto_clients


@pytest.mark.parametrize("mode", ["local", "cloud"])
def test_buddy_fact_calls_bedrock_only_outside_local_mode(set_mode, boto_clients, mode):
    set_mode(mode)
    out = asyncio.run(buddy.buddy_fact())
    assert (boto_clients == []) is (mode == "local")
    if mode == "local":
        # No stand-in fact: the widget serves its curated pack and shows why.
        assert "fact" not in out and "Local mode" in out["reason"]
    else:
        assert out["fact"]  # the bubble never empties


def _failing_run() -> dict:
    return {"run_id": 7, "branch": "b", "jobs": [{"name": "Backend", "conclusion": "failure"}]}


@pytest.fixture
def ci_calls(monkeypatch):
    """Record the gh log fetch and the diagnosis model call."""
    from server.ci import autofix

    calls: list[str] = []

    def log(*a, **k):
        calls.append("log")
        return "boom"

    def diagnose(*a, **k):
        calls.append("diagnose")
        return [{"check": "Backend", "category": "test", "summary": "s", "suggested_fix": "f"}]

    monkeypatch.setattr(autofix.provider, "failing_log", log)
    monkeypatch.setattr(autofix.diagnose, "diagnose", diagnose)
    return calls


@pytest.mark.parametrize("mode", ["local", "hybrid", "cloud"])
def test_ci_autofix_says_why_local_mode_refuses_a_cloud_diagnosis(set_mode, ci_calls, mode):
    from server.ci import autofix

    set_mode(mode, auxiliary_models={})
    plan = autofix.plan_autofix(_failing_run(), "/repo")
    if mode == "local":
        assert ci_calls == [] and plan["script"] is None and not plan["findings"]
        assert "Local mode" in plan["summary"] and "CI failure diagnosis" in plan["summary"]
    else:
        assert "diagnose" in ci_calls and plan["script"]


def test_ci_autofix_diagnoses_on_an_on_device_key_in_local_mode(set_mode, ci_calls, monkeypatch):
    from server.ci import autofix

    monkeypatch.setattr("server.local.runtime.is_local_model", lambda k: k == "local_gemma")
    set_mode("local", auxiliary_models={"ci_diagnose": "local_gemma"})
    plan = autofix.plan_autofix(_failing_run(), "/repo")
    assert "diagnose" in ci_calls and "Local mode" not in plan["summary"]


class _JsonRequest:
    def __init__(self, body: dict):
        self._body = body

    async def json(self) -> dict:
        return self._body


def test_title_sends_nothing_when_no_cloud_model_resolves(set_mode, monkeypatch, caplog):
    """Local mode titles the conversation from its first user line: no client is
    built, nothing is sent, and no failed call is logged."""
    import server.chat.routes as routes

    set_mode("local")
    built: list[str] = []
    monkeypatch.setattr(routes, "_get_bedrock_client", lambda: built.append("bedrock"))
    req = _JsonRequest({"text": "User: plan the launch\nAssistant: sure"})
    with caplog.at_level("ERROR"):
        out = asyncio.run(routes.generate_title_endpoint(req))
    assert out == {"title": "plan the launch"} and built == []
    assert not [r for r in caplog.records if r.levelname == "ERROR"]


@pytest.mark.parametrize("mode", ["local", "hybrid"])
def test_recall_ranks_on_bedrock_only_outside_local_mode(set_mode, monkeypatch, mode):
    """A cloud-model turn in Local mode (an agent, say) still keeps the memory
    manifest on this Mac: the newest memories, no selector call."""
    from server.memory import recall

    set_mode(mode)
    asked: list[str] = []

    async def selector(query, manifest, model_id, session_id=""):
        asked.append(model_id)
        return []

    monkeypatch.setattr(recall, "_query_selector", selector)
    entries = [
        ("global", SimpleNamespace(filename=f"m{i}.md", type="", mtime=0, description=""))
        for i in range(recall.MAX_SELECTIONS + 3)
    ]
    cloud_id = "global.anthropic.claude-opus-5"
    picked = asyncio.run(recall._select_entries("q", entries, model_id=cloud_id))
    if mode == "local":
        assert asked == [] and picked == entries[: recall.MAX_SELECTIONS]
    else:
        assert asked == [cloud_id]


def test_auto_mode_classifier_asks_without_a_cloud_model(set_mode, boto_clients):
    from server import auto_mode
    from server.infrastructure import config as cfg_mod

    set_mode("local")
    verdict = asyncio.run(auto_mode.classify_tool_call("ws_write_file", {}, cfg_mod.load_config()))
    assert boto_clients == []
    assert verdict["decision"] == "confirm" and "Local mode" in verdict["reason"]


def test_permission_explainer_sends_nothing_without_a_cloud_model(set_mode, boto_clients):
    from server.infrastructure import config as cfg_mod
    from server.security import explainer

    set_mode("local")
    out = asyncio.run(
        explainer.explain_permission(
            "ws_write_file", {}, [], cfg_mod.load_config(), "global.anthropic.claude-opus-5"
        )
    )
    assert out is None and boto_clients == []
