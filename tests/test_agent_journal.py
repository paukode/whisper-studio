"""Agent work survives its run: the on-disk journal, the report as the return
value, named stop reasons, salvage on cancellation, resume with context, and
delivery of reports into the session when the launching turn is gone."""

import asyncio
import dataclasses
import json
import os
from types import SimpleNamespace

import pytest

from server.agents import journal as journal_mod
from server.agents.config import get_agent_config
from server.agents.runtime import AgentResult, run_agent
from server.tasks import registry, shell
from tests.golden_harness import FakeBedrockClient, msg_end, msg_start, text_block, tool_use_block


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(journal_mod, "storage_root", lambda: str(tmp_path / "storage"))
    monkeypatch.setattr(registry, "STORAGE_DIR", str(tmp_path))
    monkeypatch.setattr(registry, "DB_PATH", str(tmp_path / "sessions.db"))
    monkeypatch.setattr(shell, "OUTPUT_DIR", str(tmp_path / "background_output"))
    monkeypatch.setattr("server.costs.budget.check_budget", lambda session_id: None)


def _patch_engine(monkeypatch, fake_stream, route_tool=None):
    monkeypatch.setattr("server.chat.engine.anthropic._get_bedrock_client", lambda: fake_stream)
    monkeypatch.setattr(
        "server.chat.tool_pool.assemble_partitioned_pool", lambda *a, **k: ([], [], 0)
    )
    monkeypatch.setattr("server.workspace.get_workspace_path", lambda: None)

    async def _default_route_tool(tool_name, tool_input, **kw):
        return "matches found", []

    monkeypatch.setattr("server.tool_executor.route_tool", route_tool or _default_route_tool)


def _tool_then_text(text: str):
    return FakeBedrockClient(
        [
            [msg_start(), *tool_use_block("t1", "ws_grep", {"pattern": "x"}), *msg_end("tool_use")],
            [msg_start(), *text_block(text), *msg_end(stop_reason="end_turn")],
        ]
    )


def _record(agent_id: str) -> dict:
    rec = journal_mod.load(agent_id)
    assert rec is not None, "journal directory missing"
    return rec


# ── the record exists from the first round and the report is the last word ──


def test_run_leaves_a_complete_record_and_returns_its_report(monkeypatch):
    _patch_engine(monkeypatch, _tool_then_text("FINDINGS: the repo has one grep hit."))
    result = asyncio.run(
        run_agent("search", agent_type="general", session_id="s1", model_id_override="m")
    )
    assert result.status == "completed" and result.stop_reason == "completed"
    assert result.output.startswith("FINDINGS: the repo has one grep hit.")

    rec = _record(result.agent_id)
    assert rec["meta"]["status"] == "completed"
    assert rec["meta"]["stop_reason"] == "completed"
    assert rec["report"].startswith("FINDINGS:")
    assert isinstance(rec["messages"], list) and rec["messages"][0]["role"] == "user"
    phases = [json.loads(line)["phase"] for line in open(os.path.join(rec["dir"], "events.jsonl"))]
    assert phases[0] == "started" and "tool_call" in phases and phases[-1] == "finished"

    row = registry.get_task(result.agent_id)
    assert row is not None and row["status"] == "completed"
    assert "one grep hit" in row["result_text"]
    assert row["output_path"].endswith("events.jsonl")


def test_turn_limit_is_named_and_the_final_round_asks_for_a_report(monkeypatch):
    _patch_engine(monkeypatch, _tool_then_text("FINDINGS: partial, two files checked."))
    cfg = dataclasses.replace(get_agent_config("general"), max_turns=2, deadline_seconds=None)
    result = asyncio.run(run_agent("search", config=cfg, session_id="s1", model_id_override="m"))
    assert result.stop_reason == "turn_limit"
    assert result.output.startswith("[Agent stopped - reached turn limit (2)]")
    assert "FINDINGS: partial" in result.output
    rec = _record(result.agent_id)
    assert rec["meta"]["stop_reason"] == "turn_limit"
    # The final-round reminder carried the report template into the transcript.
    assert "Write your final report now" in json.dumps(rec["messages"])


def test_cost_cap_gives_one_reporting_round_then_names_the_reason(monkeypatch):
    calls = {"n": 0}

    def _check_budget(session_id):
        calls["n"] += 1
        if calls["n"] == 1:
            return None
        return SimpleNamespace(
            message="cap of $1.00 reached", kind="session", limit=1.0, current=1.2
        )

    monkeypatch.setattr("server.costs.budget.check_budget", _check_budget)
    _patch_engine(monkeypatch, _tool_then_text("FINDINGS: what I had when the cap hit."))
    result = asyncio.run(
        run_agent("search", agent_type="general", session_id="s1", model_id_override="m")
    )
    assert result.stop_reason == "cost_cap"
    assert result.output.startswith("[Agent stopped - reached the session cost cap]")
    assert "FINDINGS: what I had" in result.output
    assert "cost budget for this session is reached" in json.dumps(
        _record(result.agent_id)["messages"]
    )


# ── cancellation: the harness writes the report the model could not ─────────


def test_cancelled_run_leaves_a_salvage_report(monkeypatch):
    gate = asyncio.Event()

    async def _hanging_route_tool(tool_name, tool_input, **kw):
        await gate.wait()
        return "never", []

    _patch_engine(monkeypatch, _tool_then_text("unreached"), route_tool=_hanging_route_tool)
    captured: dict = {}

    async def _run():
        task = asyncio.create_task(
            run_agent(
                "search",
                agent_type="general",
                session_id="s1",
                model_id_override="m",
                agent_id="salvage-me",
            )
        )
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        captured["done"] = True

    asyncio.run(_run())
    rec = _record("salvage-me")
    assert rec["meta"]["status"] == "stopped" and rec["meta"]["stop_reason"] == "cancelled"
    assert "cancelled before it could report" in rec["report"]
    assert "ws_grep" in rec["report"]
    assert "task_output salvage-me" in rec["report"]
    row = registry.get_task("salvage-me")
    assert row is not None and row["status"] == "stopped"
    assert "cancelled before it could report" in row["result_text"]


# ── addressable after it stopped ────────────────────────────────────────────


def test_finished_agent_resumes_with_its_context(monkeypatch):
    _patch_engine(monkeypatch, _tool_then_text("FINDINGS: the answer is 42."))
    first = asyncio.run(
        run_agent("search", agent_type="general", session_id="s1", model_id_override="m")
    )
    stored_before = len(_record(first.agent_id)["messages"])

    _patch_engine(
        monkeypatch,
        FakeBedrockClient([[msg_start(), *text_block("It was 42, from ws_grep."), *msg_end()]]),
    )
    again = asyncio.run(
        run_agent(
            "what did you find?",
            session_id="s1",
            model_id_override="m",
            resume_agent_id=first.agent_id,
        )
    )
    assert again.agent_id == first.agent_id
    assert "It was 42" in again.output
    rec = _record(first.agent_id)
    assert rec["meta"]["runs"] == 2
    assert len(rec["messages"]) > stored_before
    assert "[Follow-up from the caller after this run stopped]" in json.dumps(rec["messages"])


def test_resume_without_a_record_fails_plainly():
    result = asyncio.run(run_agent("hello", session_id="s1", resume_agent_id="ghost-agent"))
    assert result.status == "failed" and result.stop_reason == "error"
    assert "nothing to resume" in result.output


# ── the parent reads the report, not the last paragraph ─────────────────────


def _team_fixture(monkeypatch):
    from server.agent_tools import spawn as spawn_mod

    monkeypatch.setattr(spawn_mod, "_active_agent_counts", {})
    monkeypatch.setattr("server.agent_tools.spawn._record_agent_cost", lambda *a, **k: None)
    monkeypatch.setattr("server.agents.journal.team_manifest", lambda *a, **k: None)


def test_team_summary_carries_the_whole_report_and_the_relay_rule(monkeypatch):
    from server.agent_tools import teams

    _team_fixture(monkeypatch)
    long_report = (
        "FINDINGS: the leadership role is at a payments company.\n\n"
        "EVIDENCE: three job posts and a press release.\n\n"
        "CONFIDENCE: medium."
    )

    async def _fake_run_agent(task, **kwargs):
        return AgentResult(
            agent_id=kwargs["agent_id"],
            agent_type="general",
            output=long_report,
            status="completed",
            stop_reason="turn_limit",
            turns_used=7,
        )

    monkeypatch.setattr("server.agents.runtime.run_agent", _fake_run_agent)
    out, payload = asyncio.run(
        teams.execute_team_create(
            {"team_name": "research", "agents": [{"name": "a", "task": "find it"}]},
            "model-x",
            "sess1",
        )
    )
    summary = json.loads(out)["summary"]
    assert "FINDINGS: the leadership role" in summary  # the head, not the tail
    assert "CONFIDENCE: medium." in summary
    assert journal_mod.RELAY_NOTE in summary
    assert "stopped at the turn limit, 7 rounds" in summary
    assert payload["agents"][0]["stop_reason"] == "turn_limit"
    assert "task_output " + payload["agents"][0]["agent_id"] in summary


def test_cancelled_team_delivers_its_reports_to_the_session(monkeypatch):
    from server.agent_tools import teams

    _team_fixture(monkeypatch)
    monkeypatch.setattr(teams, "SALVAGE_WAIT_S", 0.2)
    delivered: list = []
    monkeypatch.setattr(
        teams, "emit_agent_report", lambda sid, payload: delivered.append((sid, payload))
    )

    async def _hanging_run_agent(task, **kwargs):
        await asyncio.Event().wait()

    monkeypatch.setattr("server.agents.runtime.run_agent", _hanging_run_agent)

    async def _run():
        task = asyncio.create_task(
            teams.execute_team_create(
                {
                    "team_name": "research",
                    "agents": [{"name": "a", "task": "find it"}, {"name": "b", "task": "check"}],
                },
                "model-x",
                "sess1",
            )
        )
        await asyncio.sleep(0.1)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(_run())
    assert len(delivered) == 1
    sid, payload = delivered[0]
    assert sid == "sess1" and payload["team_name"] == "research"
    assert [a["name"] for a in payload["agents"]] == ["a", "b"]
    assert all(a["stop_reason"] == "cancelled" for a in payload["agents"])
    assert "ended before it could read the results" in payload["reason"]


# ── the next turn sees delivered reports ────────────────────────────────────


def test_agent_report_rows_reach_the_model_as_a_user_turn():
    from server.infrastructure.sessions import visible_chat_history

    row = {
        "role": "agent_report",
        "content": "",
        "timestamp": "t",
        "agentReport": {
            "team_name": "research",
            "reason": "the turn ended first",
            "agents": [
                {
                    "name": "a",
                    "agent_id": "abc",
                    "agent_type": "general",
                    "result": "FINDINGS: it is a payments company.",
                    "status": "stopped",
                    "stop_reason": "cancelled",
                    "turns_used": 12,
                }
            ],
        },
    }
    out = visible_chat_history([{"role": "user", "content": "hi"}, row])
    assert out[1]["role"] == "user"
    assert "FINDINGS: it is a payments company." in out[1]["content"]
    assert "relay what matters" in out[1]["content"]
    assert "task_output abc" in out[1]["content"]
    assert "[cancelled, 12 rounds]" in out[1]["content"]


def test_salvage_report_names_the_reason_and_the_trail():
    text = journal_mod.salvage_report(
        "cancelled",
        texts=["Looking at the first file.", "The second file mentions Krakow."],
        tools_called=["ws_grep", "ws_grep", "web_fetch"],
        turns_used=9,
        agent_id="abc",
    )
    assert text.startswith("[Agent cancelled before it could report")
    assert "9 tool round(s)" in text
    assert "The second file mentions Krakow." in text
    assert "ws_grep x2" in text and "web_fetch x1" in text
    assert "task_output abc" in text
