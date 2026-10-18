"""The turn's input contract.

``TurnContext`` is everything a turn needs, assembled by the caller (the chat
routes, the agent runtime, cron, voice, headless runs) and handed to
runner.run_turn. The engine reads it and never builds one: no request
parsing, grounding or prompt building happens in the loop.

Pure data: nothing here imports the runner or a provider adapter.
"""

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .policy import TurnPolicy


@dataclass
class TurnContext:
    """Everything a turn needs, pre-assembled by the route (or agent/cron
    caller). The engine does no request parsing, grounding, or prompt
    building: it runs the loop."""

    session_id: str
    model_key: str
    model_id: str
    messages: list
    adapter: Any
    policy: TurnPolicy
    loop: Any
    executor: Any
    # Who is paying for this turn's rounds, recorded on every cost row
    # (server.costs.sources): chat, agent, workflow, cron, voice, headless,
    # memory, wake. Required, so no caller's spend lands unlabelled.
    cost_source: str
    plan_mode: bool = False
    mode: str = "default"
    ws_path: str | None = None
    # server.workspace.state.WorkspaceLatch taken when ws_path was read. The
    # tool batch and the gate that processes its results run under it, so
    # once the user disconnects (or switches away from) that workspace
    # mid-turn, the remaining workspace tool calls are refused with the
    # reason instead of acting on whatever is connected now. None (callers
    # that pin their own root) leaves tools unlatched.
    ws_latch: Any = None
    suppress_ws_search: bool = False
    effort_label: str | None = None
    transcript: str = ""
    current_attachments: dict = field(default_factory=dict)
    session_denials: dict = field(default_factory=dict)
    session_approvals: dict = field(default_factory=dict)
    session_config: dict = field(default_factory=dict)
    is_new_turn: bool = True
    # Model id handed to the tool executor / permission pipeline. None means
    # ctx.model_id; the local path passes "" so the permission explainer and
    # auto-mode classifier stay offline (they gate on a truthy model id).
    tool_exec_model_id: str | None = None
    # Post-turn memory hooks override; None uses the engine's generic hooks.
    # The local path supplies its own (offline-aware model_mode gating).
    memory_hooks: Callable[[list], None] | None = None
    # Callbacks into route-owned state (None outside interactive chat).
    heartbeat: Callable[[], None] | None = None
    is_disconnected: Callable[[], Any] | None = None  # async
    # True only for the user-facing chat turn (the cloud and local chat
    # routes), the one the composer is talking to. That turn alone reads
    # server.chat.engine.midturn_inbox. Subagents, cron, voice and wake turns
    # run under the same session_id, and draining there took the user's
    # message into a context nobody reads back.
    midturn_inbox: bool = False
    # Called at every round start with (round_num, messages): the agent
    # runtime snapshots the message list to its on-disk journal here, so a
    # killed run can be resumed from its last round. Errors are swallowed.
    on_round: Callable[[int, list], None] | None = None
    # Extra text appended to the reminder that forces the final round (turn
    # cap, deadline or cost cap): the agent runtime passes its report
    # template so the last message is a report, not the tail of a narration.
    final_round_hint: str | None = None
    # Live budget grant, mutated from outside the turn (server.agents.
    # extensions): {"rounds": int, "seconds": float} added to the policy's
    # round cap and deadline, read again at every round start.
    budget_extension: dict | None = None
    # True for a turn with no human attending it (subagents today; any future
    # unattended caller). Threaded into execute_tool_batch/process_tool_results
    # so approval-gated and pause-inducing tools resolve without a human:
    # WS_APPROVAL executes inline, WS_WORKSPACE_PROMPT and ask_user_question
    # refuse instead of pausing, and every dispatched call is stamped
    # __agent__ (mirrors server.agents.runtime._with_session_id).
    unattended: bool = False
    # Alternate key for turn-scoped, session_id-keyed global state (goal
    # store, the auto-mode circuit breaker, the paused-turn store, and the
    # loop-hints context tracker). None (every existing caller today) means
    # "key on session_id exactly as before", byte-identical behavior. Set
    # this when the real session_id is shared with a concurrently in-flight
    # turn (e.g. a subagent's inner turn running while the parent chat turn
    # that spawned it is still streaming): without it, the subagent's turn
    # would reset the PARENT's in-flight goal tracking / auto-mode breaker
    # and collide with its paused-approval slot.
    turn_scope_id: str | None = None
    # Channel the turn's agent progress is published on and drained from.
    # None means the session id (typed chat). A run that must not mix its
    # agent cards into a concurrent chat turn (voice) uses its own channel.
    event_channel: str | None = None
    # Per-round tool catalog override. When set, called instead of
    # _assemble_round_tools(ctx) to get (tools, core_count): lets a caller
    # (e.g. the agent runtime) supply its own filtered/precomputed tool pool
    # without the engine reaching into chat-specific tool_pool/tool_partition
    # modules. None (every existing caller today) keeps current behavior.
    tool_catalog: Callable[[], tuple[list[dict], int | None]] | None = None
    # What an agent run may execute (server.agents.tool_access.ToolScope): the
    # tool batch refuses every other name. None (every other turn): unscoped.
    tool_scope: Any = None
    # The turn's claim guard (server/chat/claim_guard.py); run_turn makes one.
    claims: Any = None
    # The deliveries earlier replies of the chat verified (their chips, sent
    # with the history): what a recap of earlier work is checked against.
    earlier_deliveries: list = field(default_factory=list)
    # What the user typed (the chat routes), kept apart from the transcript,
    # attachments and mentions inlined into the same message: the gate reads
    # it as their ask. Kept across an approval pause; None reads the message.
    asked: str | None = None
