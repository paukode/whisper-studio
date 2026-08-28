"""Bridge from the chat endpoint to the on-device runtime.

Keeps server/chat/routes.py lean (and under the file-size budget): the endpoint
calls ``local_chat_response(...)`` once, right after the system prompt + messages
are built. This function decides whether the turn is a local (on-device) one —
fresh or an approval resume — and returns the ``StreamingResponse``, or ``None``
to let the cloud Claude path proceed.

Local turns run on the unified turn engine (server/chat/engine) through the
LocalAdapter with the LOCAL policy: no completion gate, an empty tool-executor
model id (permission explainer stays offline), and compaction that summarizes
via the resident on-device model — the path stays fully offline end to end.
"""

from __future__ import annotations

from fastapi.responses import StreamingResponse


def local_chat_response(
    *,
    model_key: str,
    body: dict,
    messages: list[dict],
    session_id: str,
    approved_tool_result,
    transcript: str = "",
    whisper_md_context: str,
    memory_context: str,
    session_memory_context: str,
    plan_mode: bool,
    mode: str,
    ws_path,
    session_approvals: dict,
    session_denials: dict,
    session_config: dict,
    suppress_ws_search: bool = False,
) -> StreamingResponse | None:
    from server.local.runtime import build_local_system_prompt, is_local_model

    if not is_local_model(model_key):
        return None

    # Approval continuations flow through the SAME generic branch as the cloud
    # paths now: routes.py rebuilds the canonical messages from the unified
    # pause store before calling here, so a local resume is just a normal
    # engine turn continuing from the restored history.

    from server.local.runtime import supports_thinking, supports_tools

    # No user-facing gates: like the cloud paths, a tool-capable local model
    # always gets the full pool, and a thinking-capable one always thinks. The
    # old per-turn toggles (local_tools / local_tool_scope / local_thinking)
    # are gone; capability flags in the model registry are the only switch, so
    # a model without a capability simply doesn't use it. The full pool is a
    # stable prompt prefix, so llama-server's prefix cache absorbs the prefill
    # cost after the first turn (the 32K default context accommodates it).
    local_tools_on = supports_tools(model_key)
    thinking_on = supports_thinking(model_key)
    # Context size: the composer's CTX slider value rides on EVERY local turn,
    # so the chip is the source of truth and a lazy server start can never run
    # at a stale size. Without this, a backend restart wiped the in-memory
    # sticky value and the next turn silently relaunched llama-server at the
    # registry default while the UI showed the user's larger choice (64K chip,
    # n_ctx=32768 exceed_context_size errors). Clamped like the load endpoint —
    # never trust a client-supplied size. Recorded via set_requested_n_ctx so
    # the non-chat callers (session summariser, index extraction) reuse the
    # same size instead of bouncing the server.
    n_ctx = body.get("local_context_window")
    if n_ctx is not None:
        try:
            n_ctx = max(2048, min(int(n_ctx), 262144))
        except (TypeError, ValueError):
            n_ctx = None
    if n_ctx:
        from server.local.runtime import set_requested_n_ctx

        set_requested_n_ctx(n_ctx)
    # Lean system prompt: the cloud one is tool/workspace-heavy (~2-2.5k tokens
    # the local model can't use) — drop it to speed prefill and free n_ctx,
    # keeping only project/memory context plus, when tools are on, a short marker
    # that a workspace is connected (so the model reaches it via ws_* tools
    # instead of claiming no project was provided).
    local_system = build_local_system_prompt(
        whisper_md_context,
        memory_context,
        session_memory_context,
        tools=local_tools_on,
        ws_path=ws_path or "",
    )

    async def _engine_turn():
        import asyncio

        from server.attachment_store import load_session_attachments
        from server.chat import executor as tool_thread_pool
        from server.chat.engine.local import LocalAdapter
        from server.chat.engine.policy import LOCAL_POLICY
        from server.chat.engine.runner import TurnContext, run_turn
        from server.local import serving
        from server.local.server_stream import _spawn_memory_hooks
        from server.utils import ndjson_dumps

        # Cold start loads gigabytes; keep it off the event loop. serving
        # dispatches to the model's engine (llama-server or mlx_lm).
        try:
            base_url = await asyncio.get_running_loop().run_in_executor(
                None, serving.ensure_serving, model_key, n_ctx
            )
        except Exception as e:
            yield f"data: {ndjson_dumps({'error': str(e)})}\n\n"
            yield "data: [DONE]\n\n"
            return

        adapter = LocalAdapter(
            model_key=model_key,
            base_url=base_url,
            system_prompt=local_system,
            thinking=thinking_on,
            tools_enabled=local_tools_on,
            wire_model=serving.wire_model(model_key),
            supports_tool_choice=serving.supports_tool_choice(model_key),
        )
        current_attachments = await asyncio.to_thread(load_session_attachments, session_id)
        turn_ctx = TurnContext(
            session_id=session_id,
            model_key=model_key,
            # The local memory/cost plumbing keys on the model KEY, and the
            # engine's generic hooks are overridden below anyway.
            model_id=model_key,
            messages=messages,
            adapter=adapter,
            policy=LOCAL_POLICY,
            loop=asyncio.get_running_loop(),
            executor=tool_thread_pool,
            plan_mode=plan_mode,
            mode=mode,
            ws_path=ws_path,
            suppress_ws_search=suppress_ws_search,
            transcript=transcript,
            current_attachments=current_attachments,
            session_denials=session_denials,
            session_approvals=session_approvals,
            session_config=session_config,
            is_new_turn=approved_tool_result is None,
            # Offline invariants: no permission-explainer model, and the
            # local-aware memory hooks (model_mode gating) instead of the
            # generic cloud ones.
            tool_exec_model_id="",
            memory_hooks=lambda msgs: _spawn_memory_hooks(model_key, msgs, session_id, ws_path),
        )
        async for chunk in run_turn(turn_ctx):
            yield chunk

    return StreamingResponse(_engine_turn(), media_type="text/event-stream")
