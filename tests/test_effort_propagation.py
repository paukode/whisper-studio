"""The session's effort level must survive every hop.

One requirement, checked in the places it used to break: a turn's effort is the
same effort its subagents, its nested agents, its workflows and every agent
inside those workflows run at — clamped per model where a model cannot honour
it, never silently dropped. Plus the capability half: ultracode is orchestration
support, decoupled from the raw reasoning ladder, which is what lets Sonnet 5
offer it without pretending to accept Opus's ``xhigh`` rung.
"""

from __future__ import annotations

import asyncio
import os
import shutil

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

NODE = shutil.which("node") or "/usr/local/bin/node"
HARNESS_OK = os.path.exists(NODE) and os.path.exists(
    os.path.join(
        os.path.dirname(os.path.dirname(__file__)), "server", "workflows", "harness", "harness.mjs"
    )
)

_NOOP = "export const meta = { name: 'noop', description: 'no agents', phases: ['a'] }\nphase('a')\nreturn { ok: 1 }\n"

_SONNET5 = {"id": "global.anthropic.claude-sonnet-5", "label": "Sonnet 5"}
_OPUS5 = {"id": "global.anthropic.claude-opus-5", "label": "Opus 5", "effort_tier": "full"}
_HAIKU = {"id": "global.anthropic.claude-haiku-4-5", "label": "Haiku", "effort_tier": "none"}


# ── capability: ultracode is orchestration, not a wire value ─────────────────


def test_sonnet5_offers_ultracode_on_a_standard_ladder():
    from server.infrastructure.effort import api_effort_for, effort_levels_for, effort_tier_for

    levels = effort_levels_for(_SONNET5, "sonnet5")
    assert "ultracode" in levels
    # It gains the MODE without gaining a reasoning rung it does not have.
    assert effort_tier_for(_SONNET5, "sonnet5") == "standard"
    assert "extra" not in levels
    # ...so the wire value is its own top rung, not Opus's xhigh.
    assert api_effort_for("ultracode", _SONNET5, "sonnet5") == "max"


def test_opus_and_fable_ultracode_wire_value_is_unchanged():
    from server.infrastructure.effort import api_effort_for

    assert api_effort_for("ultracode", _OPUS5, "opus5.0") == "xhigh"
    assert api_effort_for("ultracode", {"effort_tier": "full"}, "fable5.0") == "xhigh"


def test_pre_5_models_and_haiku_still_have_no_ultracode():
    from server.infrastructure.effort import effort_levels_for

    sonnet45 = {"id": "global.anthropic.claude-sonnet-4-5"}
    assert "ultracode" not in effort_levels_for(sonnet45, "sonnet")
    assert "ultracode" not in effort_levels_for(
        {"id": "global.anthropic.claude-opus-4-7"}, "opus4.7"
    )
    assert effort_levels_for(_HAIKU, "haiku") == []


def test_capability_survives_a_renamed_config_key():
    """Capability is read from the model id too, so an entry renamed by the user
    does not quietly lose ultracode to key-spelling."""
    from server.infrastructure.effort import effort_levels_for

    assert "ultracode" in effort_levels_for({"id": "global.anthropic.claude-sonnet-5"}, "my-sonnet")


def test_explicit_config_flag_wins_both_ways():
    from server.infrastructure.effort import effort_levels_for

    off = effort_levels_for({**_SONNET5, "supports_ultracode": False}, "sonnet5")
    assert "ultracode" not in off
    on = effort_levels_for({"id": "x", "supports_ultracode": True}, "sonnet")
    assert "ultracode" in on


def test_resolve_effort_clamps_per_model_and_keeps_none_for_effortless():
    from server.infrastructure.effort import resolve_effort

    assert resolve_effort(_SONNET5, "sonnet5", "ultracode") == "ultracode"
    # A model without the mode drops to the nearest rung it does have.
    assert resolve_effort({"supports_ultracode": False}, "sonnet", "ultracode") == "max"
    assert resolve_effort({"effort_tier": "full"}, "opus5.0", "extra") == "extra"
    # Haiku has no effort at all — the ONLY legitimate None.
    assert resolve_effort(_HAIKU, "haiku", "ultracode") is None
    # An omitted level is not a decision: fall back to the default, not to None.
    assert resolve_effort(_SONNET5, "sonnet5", None) == "high"


def test_anthropic_body_sends_sonnet5s_own_top_rung_for_ultracode(monkeypatch):
    """End of the wire: an ultracode turn on Sonnet 5 must not ask Bedrock for
    a rung that model's ladder does not have."""
    from server.chat.engine import anthropic as A

    monkeypatch.setattr(A, "_get_bedrock_client", lambda: object())
    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: {"chat_model_meta": {"sonnet5": _SONNET5}},
    )
    adapter = A.AnthropicAdapter(
        model_key="sonnet5",
        model_id=_SONNET5["id"],
        system_prompt="s",
        system_static="",
        system_dynamic="",
        caching_on=False,
        cache_ttl="5m",
        effort_label="ultracode",
        force_skill=None,
        loop=None,
        executor=None,
    )
    body = adapter._build_body([{"role": "user", "content": "hi"}], None, 0, 1, True)
    assert body["thinking"] == {"type": "adaptive", "display": "summarized"}
    assert body["output_config"] == {"effort": "max"}


def test_thinking_asks_for_a_visible_summary(monkeypatch):
    """A thinking turn has to ask for the summary or it gets an empty one.

    ``display`` defaults to "omitted" on every current model, and an omitted
    display still streams a thinking block — carrying an empty string. The UI
    opens a "Thinking…" block on content_block_start and then has nothing to
    put in it, so the model looks like it is thinking silently forever. Verified
    live against Bedrock: Opus 5 and Sonnet 5 return 0 thinking characters
    without this field and a real summary with it.
    """
    from server.chat.engine import anthropic as A

    monkeypatch.setattr(A, "_get_bedrock_client", lambda: object())
    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: {"chat_model_meta": {"opus5.0": _OPUS5}},
    )

    def _body(effort_label):
        adapter = A.AnthropicAdapter(
            model_key="opus5.0",
            model_id=_OPUS5["id"],
            system_prompt="s",
            system_static="",
            system_dynamic="",
            caching_on=False,
            cache_ttl="5m",
            effort_label=effort_label,
            force_skill=None,
            loop=None,
            executor=None,
        )
        return adapter._build_body([{"role": "user", "content": "hi"}], None, 0, 1, True)

    assert _body("high")["thinking"]["display"] == "summarized"
    # Haiku's None still means no thinking block at all — not a silent one.
    assert "thinking" not in _body(None)


# ── propagation: the workflow launch paths agree ─────────────────────────────


def test_preview_card_carries_the_turns_effort():
    """The approval card is the only channel back to the launch route, so the
    effort has to ride it — otherwise approving a run reasons differently from
    letting it auto-launch."""
    from server.workflows.tools import _preview

    _, side = _preview(
        "src",
        {"name": "d", "phases": ["x"]},
        600000,
        name=None,
        args=None,
        model_id="m",
        effort_label="ultracode",
    )
    assert side[0]["workflow_preview"]["effort_label"] == "ultracode"


@pytest.mark.skipif(not HARNESS_OK, reason="node/harness missing")
class TestWorkflowLaunchEffort:
    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setenv("WHISPER_DATA_DIR", str(tmp_path / "data"))
        os.makedirs(tmp_path / "data", exist_ok=True)
        from server.infrastructure import sessions

        storage = tmp_path / "storage"
        os.makedirs(storage, exist_ok=True)
        monkeypatch.setattr(sessions, "STORAGE_DIR", str(storage))
        monkeypatch.setattr(sessions, "DB_PATH", str(storage / "sessions.db"))
        from server.security import permissions as P

        monkeypatch.setattr(P, "PERMISSIONS_PATH", str(tmp_path / "permissions.json"))
        from server.tasks import registry

        monkeypatch.setattr(registry, "STORAGE_DIR", str(storage))
        monkeypatch.setattr(registry, "DB_PATH", str(storage / "sessions.db"))
        from server.workflows import launch_policy, manager

        manager._live.clear()
        manager._task_ids.clear()
        # Launch-route tests in this class grant their script for the session;
        # the untrusted-by-name 403 test uses the same script bytes and must
        # start ungranted.
        launch_policy._session_grants.clear()
        yield
        manager._live.clear()
        manager._task_ids.clear()

    def _client(self):
        from server.workflows.routes import router

        app = FastAPI()
        app.include_router(router)
        return TestClient(app)

    def test_approved_launch_records_the_effort_it_was_given(self, monkeypatch):
        monkeypatch.setattr("server.chat.infra._get_chat_model_meta", lambda: {"sonnet5": _SONNET5})
        monkeypatch.setattr(
            "server.chat.infra._get_chat_models", lambda: {"sonnet5": _SONNET5["id"]}
        )
        c = self._client()
        r = c.post(
            "/api/workflows/runs",
            json={
                "script": _NOOP,
                "session_id": "s1",
                "model_key": "sonnet5",
                "effort_label": "ultracode",
            },
        )
        assert r.status_code == 200
        from server.workflows import manager

        run = manager.get_run(r.json()["run_id"])
        assert run["effort_label"] == "ultracode"
        assert run["model_key"] == "sonnet5"

    def test_launch_without_an_effort_falls_back_to_configured_not_none(self, monkeypatch):
        """An omitted effort is a gap in the caller, not a request for no
        reasoning — the run must still get a real level."""
        monkeypatch.setattr("server.chat.infra._get_chat_model_meta", lambda: {"sonnet5": _SONNET5})
        c = self._client()
        r = c.post("/api/workflows/runs", json={"script": _NOOP, "session_id": "s1"})
        assert r.status_code == 200
        from server.workflows import manager

        assert manager.get_run(r.json()["run_id"])["effort_label"] is not None

    def test_launch_clamps_an_effort_the_model_cannot_honour(self, monkeypatch):
        monkeypatch.setattr(
            "server.chat.infra._get_chat_model_meta",
            lambda: {"old": {"id": "global.anthropic.claude-sonnet-4-5"}},
        )
        monkeypatch.setattr(
            "server.chat.infra._get_chat_models",
            lambda: {"old": "global.anthropic.claude-sonnet-4-5"},
        )
        c = self._client()
        r = c.post(
            "/api/workflows/runs",
            json={
                "script": _NOOP,
                "session_id": "s1",
                "model_key": "old",
                "effort_label": "ultracode",
            },
        )
        from server.workflows import manager

        assert manager.get_run(r.json()["run_id"])["effort_label"] == "max"

    def test_untrusted_saved_workflow_cannot_launch_by_name(self):
        """The REST route is what /workflow <name> calls; gating trust only in
        the model-facing tool left this path able to skip approval entirely."""
        from server.workflows import store

        store.save_script("untrusted-flow", _NOOP, {"name": "untrusted-flow"}, trusted=False)
        c = self._client()
        r = c.post("/api/workflows/runs", json={"name": "untrusted-flow", "session_id": "s1"})
        assert r.status_code == 403
        assert r.json()["preview_required"] is True

        store.approve_script("untrusted-flow")
        ok = c.post("/api/workflows/runs", json={"name": "untrusted-flow", "session_id": "s1"})
        assert ok.status_code == 200


# ── propagation: workflow child agents ───────────────────────────────────────


def test_workflow_agent_inherits_the_runs_effort():
    from server.workflows.agent_adapter import _agent_effort

    assert _agent_effort({}, "ultracode") == "ultracode"
    assert _agent_effort({"label": "x"}, "max") == "max"


def test_workflow_agent_effort_speaks_one_vocabulary():
    """The script contract documents Claude Code's raw names; the app speaks
    labels. "xhigh" used to fall through every map and land on high at Anthropic
    but medium at OpenAI — the same word, two depths."""
    from server.workflows.agent_adapter import _agent_effort

    assert _agent_effort({"effort": "xhigh"}, "high") == "extra"
    assert _agent_effort({"effort": "extra"}, "high") == "extra"
    assert _agent_effort({"effort": "LOW"}, "high") == "low"


def test_workflow_agent_effort_inherits_rather_than_inventing():
    from server.workflows.agent_adapter import _agent_effort

    # A typo must not become "high" — inherit the run's level instead.
    assert _agent_effort({"effort": "banana"}, "ultracode") == "ultracode"
    assert _agent_effort({"effort": ""}, "ultracode") == "ultracode"
    # A child agent inside a workflow does not launch a second one.
    assert _agent_effort({"effort": "ultracode"}, "max") == "max"


def test_run_agent_reclamps_an_inherited_effort_to_the_child_model(monkeypatch):
    """An ultracode parent handing work to a Sonnet 4.5 child must not forward a
    label that model does not offer."""
    from server.infrastructure.effort import resolve_effort

    child = {"id": "global.anthropic.claude-sonnet-4-5"}
    assert resolve_effort(child, "sonnet", "ultracode") == "max"
    # ...and a Sonnet 5 child keeps it.
    assert resolve_effort(_SONNET5, "sonnet5", "ultracode") == "ultracode"


# ── propagation: unattended entry points ─────────────────────────────────────


def test_unattended_runs_resolve_the_configured_effort(monkeypatch):
    """cron and headless have no composer to read, but "no session" is not "no
    effort": passing None sent no thinking block at all."""
    from server.chat import infra

    monkeypatch.setattr(infra, "load_config", lambda *a, **k: {"effort_level": "max"})
    monkeypatch.setattr(infra, "_get_chat_model_meta", lambda: {"sonnet5": _SONNET5})
    assert infra.effort_for_model("sonnet5") == "max"
    # An explicit request still wins over the configured default.
    assert infra.effort_for_model("sonnet5", "low") == "low"
    # And an effortless model still resolves to None.
    monkeypatch.setattr(infra, "_get_chat_model_meta", lambda: {"haiku": _HAIKU})
    assert infra.effort_for_model("haiku") is None


def test_unreversible_model_id_does_not_guess_a_key(monkeypatch):
    """A model id that is not in the global catalogue (a workspace-only entry, a
    renamed or removed one, a stale approval card) must not resolve its effort
    against a guessed key. It used to answer "sonnet", which silently clamped an
    ultracode run down to max."""
    from server.workflows.tools import _default_model_key, _model_key_for

    monkeypatch.setattr(
        "server.infrastructure.config.load_config",
        lambda *a, **k: {
            "chat_models": {"opus5.0": "global.anthropic.claude-opus-5"},
            "default_chat_model": "opus5.0",
        },
    )
    assert _model_key_for("global.anthropic.claude-opus-5") == "opus5.0"
    assert _model_key_for("global.anthropic.claude-does-not-exist") == ""
    # ...and the caller's fallback is the configured default, never "" (an empty
    # key infers the standard ladder, clamping ultracode away just as quietly).
    assert _default_model_key() == "opus5.0"


def test_openai_children_keep_extra_which_their_ladder_omits():
    """The openai tier list has no "extra" rung because the GPT picker does not
    offer it, but the provider maps that label itself. Clamping against the list
    would demote an extra-effort parent's GPT child from xhigh to high."""
    from server.openai_bedrock.runtime import _EFFORT_MAP

    assert _EFFORT_MAP["extra"] == "xhigh"
    import inspect

    from server.agents import runtime

    src = inspect.getsource(runtime._run_agent_loop)
    assert 'effort_label is not None and _provider != "openai_bedrock"' in src


def test_malformed_effort_tier_cannot_put_a_bad_rung_on_the_wire():
    """effort_tier comes from config unvalidated. A typo used to make
    api_effort_for fall back to a model-unaware xhigh while the picker still
    offered ultracode off the standard ladder."""
    from server.infrastructure.effort import api_effort_for, effort_levels_for, resolve_effort

    typo = {"id": "global.anthropic.claude-sonnet-5", "effort_tier": "standrd"}
    assert "ultracode" in effort_levels_for(typo, "sonnet5")
    assert resolve_effort(typo, "sonnet5", "ultracode") == "ultracode"
    # The wire value follows the SAME fallback ladder the picker was built from.
    assert api_effort_for("ultracode", typo, "sonnet5") == "max"


def test_adapter_uses_the_metadata_the_effort_was_resolved_from(monkeypatch):
    """The label is resolved from the latched, workspace-aware snapshot. The wire
    value must come from that same snapshot, or a workspace-only model gets a
    label from one config and a rung from another."""
    from server.chat.engine import anthropic as A

    monkeypatch.setattr(A, "_get_bedrock_client", lambda: object())
    # Global config does NOT know this key — only the passed-in meta does.
    monkeypatch.setattr(
        "server.infrastructure.config.load_config", lambda *a, **k: {"chat_model_meta": {}}
    )
    adapter = A.AnthropicAdapter(
        model_key="workspace-opus",
        model_id="global.anthropic.claude-opus-5",
        system_prompt="s",
        system_static="",
        system_dynamic="",
        caching_on=False,
        cache_ttl="5m",
        effort_label="ultracode",
        force_skill=None,
        loop=None,
        executor=None,
        meta={"id": "global.anthropic.claude-opus-5", "effort_tier": "full"},
    )
    body = adapter._build_body([{"role": "user", "content": "hi"}], None, 0, 1, True)
    assert body["output_config"] == {"effort": "xhigh"}


def test_subagent_endpoint_resolves_an_effort():
    """/subagent was the last agent entry point passing nothing at all."""
    import inspect

    from server.chat import routes

    src = inspect.getsource(routes.subagent_stream_endpoint)
    assert "effort_for_model(model_key" in src


def test_cron_and_headless_no_longer_hardcode_no_effort():
    """Guard the specific regression: both files passed effort_label=None into
    their adapters, so every scheduled turn ran without adaptive thinking."""
    import inspect

    from server import cron_run
    from server.exec import headless

    for mod in (cron_run, headless):
        src = inspect.getsource(mod)
        assert "effort_label=None" not in src, f"{mod.__name__} still hardcodes no effort"
        assert "effort_for_model" in src, f"{mod.__name__} does not resolve an effort"
