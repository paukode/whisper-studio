"""On-device model metadata and prompts — fully isolated from Bedrock/Claude.

The weights themselves live in a llama-server subprocess (see
server/local/llama_server.py); this module owns everything *around* that: the
config-driven registry view, GGUF download/paths, the sticky context-size
request, capability flags, and the local system prompt. Only one model is
resident at a time, matching the memory-constrained on-device build (e.g.
M3/18GB).

There is deliberately no in-process engine any more. llama-server is the single
on-device backend, so a model added to config.json is served through upstream's
own template and tool-call parsers rather than marker code maintained here — and
when it is unavailable, local turns fail loudly instead of silently degrading.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Mapping

from server.infrastructure.paths import models_root

log = logging.getLogger("whisper-studio")

# models/ lives under the app home (WHISPER_HOME in a packaged install, the
# repo root in a dev checkout), alongside the ASR models.
MODELS_DIR = models_root()


# Supported on-device models. Keys are prefixed ``local_`` by convention so they
# read as on-device at a glance; detection itself is registry membership (below),
# not the prefix, so a config-added model does not have to follow it.
# The registry is config-driven — see server/local/registry.py. This module-level
# name is kept as a LIVE VIEW for the many callsites that read it like a dict
# (``key in LOCAL_MODELS``, ``LOCAL_MODELS[key]["ctx"]``, iteration): it resolves
# the merged built-in + config registry on every access, so a model added to
# config.json is picked up without a restart and without touching this file.
class _LocalModelsView(Mapping):
    """Read-only Mapping over the effective registry (built-ins + config)."""

    def _resolved(self) -> dict[str, dict]:
        from server.local.registry import local_models

        return local_models()

    def __getitem__(self, key):
        return self._resolved()[key]

    def __iter__(self):
        return iter(self._resolved())

    def __len__(self):
        return len(self._resolved())

    def __repr__(self):  # pragma: no cover - debugging aid
        return f"_LocalModelsView({self._resolved()!r})"


LOCAL_MODELS: Mapping[str, dict] = _LocalModelsView()

# The context window (n_ctx) most recently REQUESTED by an explicit load — i.e.
# the size the user picked in the chat-input context-window slider, which loads
# the model via /api/local-model/load?n_ctx=... . A chat turn only asks that the
# model be resident (n_ctx=None); without remembering the choice here, None
# resolves back to the model default and restarts the server smaller mid-turn, so
# the very next message overflows even though the badge says 32K/64K. Keeping it
# sticky makes None mean "keep whatever the user last asked for". Updated only by
# an explicit request, so a lazy start can never downshift the resident window.
_requested_n_ctx: int | None = None


def requested_n_ctx() -> int | None:
    """The context size the user last explicitly asked for, or None.

    Read by the model server when deciding what window to start at, so the
    chat-input slider survives a lazy start later in the session.
    """
    return _requested_n_ctx


def set_requested_n_ctx(n: int | None) -> None:
    """Record an explicit context-size request (slider / API). Ignores None so a
    lazy load can never clear the user's choice."""
    global _requested_n_ctx
    if n is not None:
        _requested_n_ctx = int(n)


def is_local_model(key: str | None) -> bool:
    return bool(key) and key in LOCAL_MODELS


# All on-device model *ids* (the resolved sentinel, not the key) start with this
# prefix. It is the single discriminator shared cloud-path code uses to stay
# offline for local turns — centralised here so a sentinel change can't silently
# re-enable Bedrock calls in callers like memory recall.
_LOCAL_ID_PREFIX = "local:"


def is_local_model_id(model_id: str | None) -> bool:
    """True if a resolved model *id* is an on-device sentinel (``local:...``)."""
    return bool(model_id) and model_id.startswith(_LOCAL_ID_PREFIX)


def key_for_id(model_id: str | None) -> str | None:
    """Resolve a local model *id* (sentinel) back to its registry key, or None.
    Lets cloud-path code that only has the model_id reach the local runtime."""
    if not model_id:
        return None
    for k, m in LOCAL_MODELS.items():
        if m["id"] == model_id:
            return k
    return None


def local_model_meta(key: str) -> dict:
    return LOCAL_MODELS.get(key, {})


def gguf_path(key: str) -> str:
    m = LOCAL_MODELS[key]
    return os.path.join(MODELS_DIR, m["dir"], m["filename"])


def is_downloaded(key: str) -> bool:
    return is_local_model(key) and os.path.exists(gguf_path(key))


def ensure_downloaded(key: str) -> str:
    """Fetch the GGUF into models/ if absent. Returns the local path."""
    path = gguf_path(key)
    if os.path.exists(path):
        # Positive confirmation so "did it download or just load?" is obvious.
        log.info("Local model %s already present at %s — loading from disk.", key, path)
        return path
    from huggingface_hub import hf_hub_download

    m = LOCAL_MODELS[key]
    log.info("Downloading local model %s (%s) into %s ...", key, m["filename"], MODELS_DIR)
    hf_hub_download(
        repo_id=m["repo_id"],
        filename=m["filename"],
        local_dir=os.path.join(MODELS_DIR, m["dir"]),
    )
    log.info("Local model %s download complete.", key)
    return path


def is_loaded(key: str | None = None) -> bool:
    """Whether a local model (or ``key`` specifically) is currently resident.

    Residency is a property of the model server, not this process — the weights
    were never in here.
    """
    from server.local import llama_server

    resident = llama_server.resident_key()
    if resident is None:
        return False
    return True if key is None else resident == key


def loaded_key() -> str | None:
    """The key of the currently resident local model, or None.
    Lets callers follow the active model (e.g. the one-shot map step) instead of
    pinning a fixed key and evicting the resident one to load it."""
    from server.local import llama_server

    return llama_server.resident_key()


def complete(key: str, system_prompt: str, user: str, max_tokens: int = 1500) -> str:
    """One-shot, NON-streaming generation; returns the assistant text ('' on
    failure). Blocking — starts the model server if needed, so call it off the
    event loop.

    Kept here as the stable entry point for non-chat callers (workspace-index
    relations/descriptions/contextualize, the one-shot helper, the on-device
    session summariser); the work itself happens in the model server.
    """
    if not is_local_model(key):
        return ""
    from server.local import llama_server

    return llama_server.complete(key, system_prompt, user, max_tokens)


_LOCAL_SYSTEM_BASE = (
    "You are a helpful, concise assistant running locally on the user's device. "
    "Answer the user directly and clearly. You do not have access to tools, the "
    "workspace filesystem, or the internet, so do not claim to use them."
)

# When tools are enabled we must NOT tell the model it has no tools/internet —
# that makes it refuse instead of calling the declared tools. This prompt does
# the opposite: it tells the model it has a full toolset (declared separately in
# the chat template) and to use it. The exact tool list is rendered by the
# template, so we describe capabilities rather than enumerate every tool.
_LOCAL_SYSTEM_TOOLS = (
    "You are a capable assistant running locally on the user's device, with a set "
    "of tools available to you. The tools you may use are declared in this "
    "conversation; rely on those rather than assuming. Depending on what is "
    "enabled they may let you search and read the web, read and search the "
    "workspace, inspect code, run git, manage tasks and memory, and edit or create "
    "files. Use the declared tools whenever they help — DO NOT claim you lack "
    "tools, internet, or a filesystem when a matching tool is declared. To use a "
    "tool, emit a tool call; then answer using the result. Prefer reading and "
    "searching before answering questions about the user's code or files. Actions "
    "that change files or run commands will pause for the user's approval before "
    "they take effect, so propose them when needed; the user will approve or "
    "reject. Call only the tools you actually need, and stop calling tools once "
    "you can answer."
)


def _local_workspace_note(ws_path: str) -> str:
    """A trimmed workspace marker for the local prompt.

    The cloud ``workspace_prompt`` is ~400 tokens of write/approval workflow the
    on-device model doesn't need spelled out. This is the one thing it DOES need:
    that a workspace is connected right now and it should reach it via ws_* tools
    instead of telling the user no project was provided."""
    return (
        f"A code workspace is connected at '{ws_path}'. When the user asks about "
        "their project, code, or files, use the ws_* tools (ws_read_file, ws_grep, "
        "ws_glob, ws_list_directory) to read and search it — do NOT say that no "
        "file, folder, or project was provided. Read files before answering "
        "questions about them."
    )


def build_local_system_prompt(
    whisper_md: str = "",
    memory: str = "",
    session_memory: str = "",
    tools: bool = False,
    ws_path: str = "",
) -> str:
    """A lean system prompt for local turns.

    The cloud system prompt carries ~2-2.5k tokens of tool/workspace/skill
    guidance the local runtime can't act on — feeding it to a small on-device
    model just slows prefill (a longer warm-up) and eats the n_ctx budget. Keep
    a short identity plus any genuinely useful project/memory context. When
    ``tools`` is set, use the tools-positive identity instead of the no-tools one.

    When a workspace is connected AND tools are on, append a short marker so the
    model knows to reach the codebase with ws_* tools. Without tools there are no
    ws_* tools to call, so the marker is skipped — the honest no-tools identity
    stands.
    """
    from server.prompts.rules import append_rules

    parts = [_LOCAL_SYSTEM_TOOLS if tools else _LOCAL_SYSTEM_BASE]
    if tools and ws_path:
        parts.append(_local_workspace_note(ws_path))
    for extra in (whisper_md, memory, session_memory):
        if extra and extra.strip():
            parts.append(extra.strip())
    return append_rules("\n\n".join(parts))


def supports_thinking(key: str) -> bool:
    return bool(LOCAL_MODELS.get(key, {}).get("supports_thinking"))


def supports_tools(key: str) -> bool:
    return bool(LOCAL_MODELS.get(key, {}).get("supports_tools"))
