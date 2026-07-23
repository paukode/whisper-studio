"""On-device LLM runtime (local mode) — fully isolated from Bedrock/Claude.

Runs a GGUF model via llama-cpp-python on Apple Silicon (Metal). Only one model
is resident at a time: loading a different local model unloads the previous one,
matching the memory-constrained on-device build (e.g. M3/18GB). This path is
reached only when a ``local_*`` model key is selected; the cloud Claude path in
server/chat is never touched.

llama-cpp-python is imported lazily so the server still boots (and the cloud
models work) on machines where the local runtime isn't installed — selecting a
local model there surfaces a clear "run setup.sh --prod" error instead.
"""

from __future__ import annotations

import logging
import os
import threading
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger("whisper-studio")

# This file is server/local/runtime.py, so the repo root is THREE levels up
# (local → server → root). models/ lives at the repo root, alongside the ASR
# models — keep this in sync if the file ever moves deeper/shallower.
SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
MODELS_DIR = os.path.join(SCRIPT_DIR, "models")

# Supported on-device models. Keys are prefixed ``local_`` so the chat router
# can detect them by key alone, with no config lookup. `id` is a sentinel that
# never reaches Bedrock (the router branches before any AWS call).
LOCAL_MODELS: dict[str, dict] = {
    "local_gemma": {
        "id": "local:gemma-4-12b-it-qat-q4_0",
        "label": "Gemma 4 12B (Local)",
        "repo_id": "google/gemma-4-12B-it-qat-q4_0-gguf",
        "filename": "gemma-4-12b-it-qat-q4_0.gguf",
        "dir": "gemma-4-12b-it-qat-q4_0",
        # Gemma 4 supports up to 262144 (256K) natively. 16K is the default and
        # the floor: with tools on, the tool-pool prompt alone is ~12K tokens, so
        # a smaller window overflows. The user can raise it live from the
        # chat-input context-window slider (which reloads the model at the new
        # size, prompting for confirmation above 16K). WHISPER_LOCAL_N_CTX still
        # overrides the default at startup.
        "ctx": 16384,
        "supports_thinking": True,
        "supports_tools": True,
    },
    "local_gemma_coder": {
        "id": "local:gemma-4-12b-coder",
        "label": "Gemma 4 Coder (Local)",
        "repo_id": "yuxinlu1/gemma-4-12B-coder-fable5-composer2.5-v1-GGUF",
        "filename": "gemma4-coding-Q4_K_M.gguf",
        "dir": "gemma-4-12b-coder",
        # Same family as Gemma 4 12B above; identical context + capability flags.
        "ctx": 16384,
        "supports_thinking": True,
        "supports_tools": True,
    },
}

# All llama.cpp work (load, generate, unload) runs on ONE thread so the model
# is created and used on the same thread — mirrors the ASR backends.
executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="local-llm")

_llm = None  # the loaded llama_cpp.Llama instance
_llm_key: str | None = None
_lock = threading.Lock()


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
    if key is None:
        return _llm is not None
    return _llm is not None and _llm_key == key


def loaded_key() -> str | None:
    """The key of the currently resident local model, or None if none is loaded.
    Lets callers follow the active model (e.g. the one-shot map step) instead of
    pinning a fixed key and evicting the resident one to load it."""
    return _llm_key


def load_sync(key: str, n_ctx: int | None = None) -> None:
    """Load the GGUF into memory (unloading any other local model — or the same
    model at a different context size — first).

    ``n_ctx`` overrides the context window for this load; None falls back to the
    WHISPER_LOCAL_N_CTX env var, else the model's default (16K). Because
    llama.cpp fixes n_ctx at construction, changing it requires a full reload —
    that is how the UI's context-window slider grows the window: it re-creates
    the model at the requested size.

    MUST run on ``executor`` (model has thread affinity). Raises RuntimeError
    with an actionable message when the runtime or weights are missing.
    """
    global _llm, _llm_key
    if not is_local_model(key):
        raise RuntimeError(f"Unknown local model: {key}")
    m = LOCAL_MODELS[key]
    # Larger n_ctx = larger KV cache = more memory; flash attention keeps it in
    # check and is needed for efficient sliding-window attention.
    target_n_ctx = (
        int(n_ctx)
        if n_ctx is not None
        else int(os.environ.get("WHISPER_LOCAL_N_CTX") or m.get("ctx", 16384))
    )
    # Same model already resident at the same context size → nothing to do.
    if (
        _llm is not None
        and _llm_key == key
        and getattr(_llm, "_studio_n_ctx", None) == target_n_ctx
    ):
        return
    with _lock:
        # Reload when the model differs OR the requested context size changed
        # (llama.cpp can't resize n_ctx in place).
        if _llm is not None and (
            _llm_key != key or getattr(_llm, "_studio_n_ctx", None) != target_n_ctx
        ):
            _unload_locked()
        if _llm is None:
            try:
                from llama_cpp import Llama
            except ImportError as e:
                raise RuntimeError(
                    "llama-cpp-python is not installed — run `bash setup.sh --prod` "
                    "to set up on-device models."
                ) from e
            path = ensure_downloaded(key)
            log.info("Loading local model %s into memory (n_ctx=%d) ...", key, target_n_ctx)
            _llm = Llama(
                model_path=path,
                n_ctx=target_n_ctx,
                n_gpu_layers=-1,  # offload all layers to Metal on Apple Silicon
                flash_attn=True,  # smaller KV cache, faster; aids gemma's SWA
                verbose=False,
            )
            # Stash the REQUESTED n_ctx for the reload comparison above —
            # llama_cpp may pad the effective value and `_llm.n_ctx` is a method,
            # not the requested arg, so comparing against it would never match.
            _llm._studio_n_ctx = target_n_ctx
            _llm_key = key
            log.info("Local model %s loaded.", key)


def _unload_locked() -> None:
    global _llm, _llm_key
    # Free the C / Metal allocations DETERMINISTICALLY before dropping the
    # reference. ``_llm = None`` + gc alone is unsafe: any lingering reference
    # (a chat formatter, an in-flight generation, an internal llama_cpp handle)
    # keeps the ~7 GB of weights + Metal KV cache resident, so loading a second
    # 12B model on top momentarily needs BOTH and OOMs an 18 GB machine —
    # freezing or crashing the system. close() releases the model immediately,
    # regardless of any remaining Python references, so the new model loads
    # into freed memory.
    if _llm is not None:
        log.info("Unloading local model %s (freeing memory) ...", _llm_key)
        try:
            _llm.close()
        except Exception as e:
            log.warning("local model close() failed during unload: %s", e)
    _llm = None
    _llm_key = None
    import gc

    gc.collect()


def unload_sync() -> None:
    """Free the resident local model. MUST run on ``executor``."""
    with _lock:
        _unload_locked()


def _complete_on_thread(key: str, system_prompt: str, user: str, max_tokens: int) -> str:
    load_sync(key)
    chat = _to_chat_messages(system_prompt, [{"role": "user", "content": user}])
    resp = _llm.create_chat_completion(
        messages=chat,
        stream=False,
        max_tokens=max_tokens,
        temperature=0.0,
    )
    try:
        return resp["choices"][0]["message"].get("content") or ""
    except (KeyError, IndexError, TypeError):
        return ""


def complete(key: str, system_prompt: str, user: str, max_tokens: int = 1500) -> str:
    """One-shot, NON-streaming generation; returns the assistant text ('' on
    failure). Blocking and safe to call from any thread — the work is submitted
    to the single model-affine ``executor`` and therefore serialises with chat
    generation. Loads the model if it isn't resident. Used by the workspace
    index's on-device typed-relation extraction."""
    if not is_local_model(key):
        return ""
    return executor.submit(_complete_on_thread, key, system_prompt, user, max_tokens).result()


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


def build_local_system_prompt(
    whisper_md: str = "", memory: str = "", session_memory: str = "", tools: bool = False
) -> str:
    """A lean system prompt for local turns.

    The cloud system prompt carries ~2-2.5k tokens of tool/workspace/skill
    guidance the local runtime can't act on — feeding it to a small on-device
    model just slows prefill (a longer warm-up) and eats the n_ctx budget. Keep
    a short identity plus any genuinely useful project/memory context. When
    ``tools`` is set, use the tools-positive identity instead of the no-tools one.
    """
    from server.prompts.rules import append_rules

    parts = [_LOCAL_SYSTEM_TOOLS if tools else _LOCAL_SYSTEM_BASE]
    for extra in (whisper_md, memory, session_memory):
        if extra and extra.strip():
            parts.append(extra.strip())
    return append_rules("\n\n".join(parts))


def _to_chat_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Flatten the Bedrock-shaped messages into llama.cpp chat messages.

    Text-only: image blocks are dropped (the local runtime here doesn't do
    vision), and tool/thinking blocks never appear because the local path is
    requested without tools.
    """
    chat: list[dict] = []
    if system_prompt:
        chat.append({"role": "system", "content": system_prompt})
    for msg in messages:
        role = msg.get("role", "user")
        if role not in ("user", "assistant", "system"):
            role = "user"
        content = msg.get("content", "")
        if isinstance(content, list):
            text = "\n".join(
                b.get("text", "")
                for b in content
                if isinstance(b, dict) and b.get("type") == "text"
            )
        else:
            text = str(content)
        if text:
            chat.append({"role": role, "content": text})
    return chat


def supports_thinking(key: str) -> bool:
    return bool(LOCAL_MODELS.get(key, {}).get("supports_thinking"))


# Gemma's reasoning is emitted in a thought channel: "<|channel>thought\n
# ...reasoning...<channel|>" followed by the answer (see the model's
# chat_template.jinja). Enabled by rendering the template with
# enable_thinking=True (which injects the <|think|> token).
_THOUGHT_OPEN = "<|channel>thought"
_THOUGHT_CLOSE = "<channel|>"


class _ThoughtSplitter:
    """Streaming splitter that separates gemma's thought channel from the
    answer. Feed text chunks; get back ('thinking', s) / ('text', s) pieces.
    Holds back a few chars so a marker split across chunks is still matched."""

    def __init__(self) -> None:
        self._buf = ""
        self._in_thought = False
        self._hold = max(len(_THOUGHT_OPEN), len(_THOUGHT_CLOSE)) - 1

    def feed(self, chunk: str) -> list[tuple[str, str]]:
        self._buf += chunk
        out: list[tuple[str, str]] = []
        while True:
            if not self._in_thought:
                idx = self._buf.find(_THOUGHT_OPEN)
                if idx == -1:
                    safe = len(self._buf) - self._hold
                    if safe > 0:
                        out.append(("text", self._buf[:safe]))
                        self._buf = self._buf[safe:]
                    break
                if idx > 0:
                    out.append(("text", self._buf[:idx]))
                rest = self._buf[idx + len(_THOUGHT_OPEN) :]
                self._buf = rest[1:] if rest.startswith("\n") else rest
                self._in_thought = True
            else:
                idx = self._buf.find(_THOUGHT_CLOSE)
                if idx == -1:
                    safe = len(self._buf) - self._hold
                    if safe > 0:
                        out.append(("thinking", self._buf[:safe]))
                        self._buf = self._buf[safe:]
                    break
                if idx > 0:
                    out.append(("thinking", self._buf[:idx]))
                self._buf = self._buf[idx + len(_THOUGHT_CLOSE) :]
                self._in_thought = False
        return out

    def flush(self) -> list[tuple[str, str]]:
        if not self._buf:
            return []
        kind = "thinking" if self._in_thought else "text"
        piece, self._buf = self._buf, ""
        return [(kind, piece)]


def _render_prompt(
    chat: list[dict], enable_thinking: bool = False, tools: list[dict] | None = None
) -> str | None:
    """Render gemma's chat template via llama-cpp's Jinja2 formatter so we can
    feed the raw prompt to create_completion (create_chat_completion can't pass
    enable_thinking, and the gemma tool block needs the template). Returns None
    on any problem → caller falls back. Self-verifies that the requested feature
    actually rendered (the <|think|> / <|tool> tokens)."""
    try:
        from llama_cpp.llama_chat_format import Jinja2ChatFormatter

        tmpl = (getattr(_llm, "metadata", None) or {}).get("tokenizer.chat_template")
        if not tmpl:
            return None
        # bos_token="" → template emits no <bos> text; create_completion adds the
        # BOS token itself, avoiding a double BOS.
        fmt = Jinja2ChatFormatter(
            template=tmpl, eos_token="<eos>", bos_token="", add_generation_prompt=True
        )
        res = fmt(messages=chat, tools=tools, enable_thinking=enable_thinking)
        prompt = getattr(res, "prompt", None)
        if not prompt or "<|turn>" not in prompt:
            return None
        if enable_thinking and "<|think|>" not in prompt:
            log.info("enable_thinking did not inject <|think|>; falling back.")
            return None
        if tools and "<|tool>" not in prompt:
            log.info("tools did not render into the prompt; falling back.")
            return None
        return prompt
    except Exception as e:
        log.warning("Local prompt render failed (%s); falling back", e)
        return None


def iter_chat(
    key: str,
    system_prompt: str,
    messages: list[dict],
    max_tokens: int = 4096,
    thinking: bool = False,
    cancel: threading.Event | None = None,
):
    """Yield (kind, text) pieces for a chat turn, kind in {"text","thinking"}.
    Loads the model if needed. Blocking generator — call on ``executor``; the
    async SSE layer in server/local/stream.py bridges the pieces out.

    ``cancel`` is a cooperative stop signal: the async bridge sets it when the
    client disconnects / hits Stop, and we break out of the decode loop at the
    next token so the single model thread is freed promptly instead of running
    on to ``max_tokens`` (which would wedge the next turn — see server/local/
    stream.py). None → run to completion (the non-streaming/happy path)."""
    load_sync(key)
    chat = _to_chat_messages(system_prompt, messages)

    if thinking and supports_thinking(key):
        prompt = _render_prompt(chat, enable_thinking=True)
        if prompt:
            log.info("Local thinking enabled for this turn.")
            splitter = _ThoughtSplitter()
            # Gemma 4 ends a turn with <turn|> (not <end_of_turn>); keep the
            # others as belt-and-suspenders stop strings.
            for out in _llm.create_completion(
                prompt=prompt,
                stream=True,
                max_tokens=max_tokens,
                stop=["<turn|>", "<end_of_turn>", "<eos>"],
            ):
                if cancel is not None and cancel.is_set():
                    break
                try:
                    piece = out["choices"][0].get("text")
                except (KeyError, IndexError):
                    piece = None
                if piece:
                    for kv in splitter.feed(piece):
                        yield kv
            for kv in splitter.flush():
                yield kv
            return

    # Default path: no thinking. Let llama.cpp format via the embedded template.
    for out in _llm.create_chat_completion(messages=chat, stream=True, max_tokens=max_tokens):
        if cancel is not None and cancel.is_set():
            break
        try:
            piece = out["choices"][0]["delta"].get("content")
        except (KeyError, IndexError):
            piece = None
        if piece:
            yield ("text", piece)


def supports_tools(key: str) -> bool:
    return bool(LOCAL_MODELS.get(key, {}).get("supports_tools"))


def to_chat_messages(system_prompt: str, messages: list[dict]) -> list[dict]:
    """Public wrapper around the message flattener — the async tool loop builds
    its conversation here, then feeds rounds back through ``generate_round``."""
    return _to_chat_messages(system_prompt, messages)


# A tool call begins with this marker; everything from it onward is the
# <|tool_call>...<tool_call|> DSL, which must NOT be shown to the user.
_TOOL_OPEN = "<|tool_call>"


class _ToolCallSplitter:
    """Streaming splitter for the tool loop: emits displayable text until a
    tool-call marker begins, then withholds the rest (the DSL). Accumulates the
    full raw text so the caller can parse the call. Holds back a few chars at the
    tail so a marker split across chunks is still caught."""

    def __init__(self) -> None:
        self._buf = ""
        self._raw = ""
        self._in_call = False
        self._hold = len(_TOOL_OPEN) - 1

    def feed(self, chunk: str) -> list[str]:
        self._raw += chunk
        if self._in_call:
            return []
        self._buf += chunk
        idx = self._buf.find(_TOOL_OPEN)
        if idx != -1:
            self._in_call = True
            pre = self._buf[:idx]
            self._buf = ""
            return [pre] if pre else []
        safe = len(self._buf) - self._hold
        if safe > 0:
            out = self._buf[:safe]
            self._buf = self._buf[safe:]
            return [out]
        return []

    def flush(self) -> list[str]:
        if self._in_call or not self._buf:
            return []
        piece, self._buf = self._buf, ""
        return [piece]

    @property
    def raw(self) -> str:
        return self._raw


def iter_generate_round(
    key: str,
    convo: list[dict],
    tool_schemas: list[dict],
    max_tokens: int = 4096,
    cancel: threading.Event | None = None,
):
    """Run ONE agentic round, STREAMING it. Yields ``("text", piece)`` for
    displayable text as it decodes (everything before any ``<|tool_call>`` DSL),
    then a final ``("raw", full_text)`` so the caller can parse the tool call.

    Streaming (vs buffering the whole round) is what keeps a tools-on turn from
    looking frozen while the model prefills a large prompt and decodes the
    answer. If the template can't render the tools, we degrade to a plain answer.
    Blocking generator — MUST run on ``executor`` (model thread affinity); the
    async loop in server/local/stream.py bridges it to SSE via a queue.

    ``cancel`` is a cooperative stop signal set by that async bridge when the
    client disconnects / hits Stop. Because generation runs on the SINGLE model
    thread, an abandoned round left to run on to ``max_tokens`` would wedge the
    next turn — so we check the flag each token and break, which stops pulling
    from the llama.cpp generator (the C decode only advances on the next pull)
    and lets the producer thread return. We still yield the flushed tail + the
    accumulated ``raw`` so a break leaves the generator well-formed; the drained
    consumer is already gone, so those trailing pieces are simply discarded."""
    load_sync(key)
    prompt = _render_prompt(convo, tools=tool_schemas)
    splitter = _ToolCallSplitter()
    if prompt:
        stream = _llm.create_completion(
            prompt=prompt,
            stream=True,
            max_tokens=max_tokens,
            stop=["<tool_call|>", "<turn|>", "<eos>"],
        )
        for out in stream:
            if cancel is not None and cancel.is_set():
                break
            try:
                piece = out["choices"][0].get("text")
            except (KeyError, IndexError):
                piece = None
            if piece:
                for t in splitter.feed(piece):
                    yield ("text", t)
    else:
        # Tools couldn't be rendered into the template — answer without tools.
        log.info("Local tools could not be rendered; answering without tools.")
        for out in _llm.create_chat_completion(messages=convo, stream=True, max_tokens=max_tokens):
            if cancel is not None and cancel.is_set():
                break
            try:
                piece = out["choices"][0]["delta"].get("content")
            except (KeyError, IndexError):
                piece = None
            if piece:
                for t in splitter.feed(piece):
                    yield ("text", t)
    for t in splitter.flush():
        yield ("text", t)
    yield ("raw", splitter.raw)


def generate_round(
    key: str, convo: list[dict], tool_schemas: list[dict], max_tokens: int = 4096
) -> str:
    """Buffered variant of :func:`iter_generate_round` — returns the full raw
    text (including any ``<|tool_call>`` DSL). Used by the on-device session
    summariser, which wants the whole summary, not a stream."""
    raw = ""
    for kind, piece in iter_generate_round(key, convo, tool_schemas, max_tokens):
        if kind == "raw":
            raw = piece
    return raw
