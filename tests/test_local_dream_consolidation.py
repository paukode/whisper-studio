"""Local-path dream consolidation hook.

The on-device chat path fed session memory + auto-memory at end of turn but
never recorded sessions for dream consolidation, so local-heavy usage never
accrued toward a consolidation pass. The local path now spawns the same
``record_and_maybe_dream`` hook the cloud paths run (chat/routes.py,
openai_bedrock/stream.py), skipping gracefully when the app is fully offline
(model_mode=local, the consolidator agent needs a cloud model).
"""

import asyncio

import server.infrastructure.feature_flags as FF
import server.local.runtime as L
import server.local.stream as STREAM


def _capture_dream(monkeypatch):
    """Route the spawned dream coroutine into a list of call kwargs."""
    import server.infrastructure.async_tasks as AT
    import server.memory.dream as DM

    calls = []

    async def fake_record(ws_path, *, model_id):
        calls.append({"ws_path": ws_path, "model_id": model_id})

    monkeypatch.setattr(DM, "record_and_maybe_dream", fake_record)
    monkeypatch.setattr(AT, "spawn", lambda coro, *, name=None: asyncio.run(coro))
    return calls


def test_dream_hook_fires_in_hybrid_mode(monkeypatch):
    import server.infrastructure.model_mode as MM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "dream_consolidation")
    monkeypatch.setattr(MM, "current_mode", lambda config=None: "hybrid")
    calls = _capture_dream(monkeypatch)

    STREAM._spawn_dream("local_gemma", "/ws")

    assert len(calls) == 1
    assert calls[0]["ws_path"] == "/ws"
    # The local sentinel id is threaded (the consolidator agent resolves its
    # own model), matching the extraction hook.
    assert calls[0]["model_id"] == L.local_model_meta("local_gemma").get("id", "")


def test_dream_hook_fires_without_workspace(monkeypatch):
    """No workspace still records toward the global tier (parity with the
    cloud paths, which spawn the hook with ws_path=None too)."""
    import server.infrastructure.model_mode as MM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "dream_consolidation")
    monkeypatch.setattr(MM, "current_mode", lambda config=None: "hybrid")
    calls = _capture_dream(monkeypatch)

    STREAM._spawn_dream("local_gemma", None)

    assert len(calls) == 1
    assert calls[0]["ws_path"] is None


def test_dream_hook_skips_when_fully_offline(monkeypatch):
    import server.infrastructure.model_mode as MM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: flag == "dream_consolidation")
    monkeypatch.setattr(MM, "current_mode", lambda config=None: "local")
    calls = _capture_dream(monkeypatch)

    STREAM._spawn_dream("local_gemma", "/ws")
    assert calls == []


def test_dream_hook_gated_on_dream_consolidation_flag(monkeypatch):
    import server.infrastructure.model_mode as MM

    monkeypatch.setattr(FF, "is_enabled", lambda flag: False)
    monkeypatch.setattr(MM, "current_mode", lambda config=None: "hybrid")
    calls = _capture_dream(monkeypatch)

    STREAM._spawn_dream("local_gemma", "/ws")
    assert calls == []
