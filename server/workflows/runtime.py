"""WorkflowRun — one harness subprocess + the RPC pump that enforces every
server-side limit the script cannot be trusted to honor.

Responsibilities:
  - spawn the Node harness (scrubbed env, own process group, 10MB line limit)
  - pump ndjson JSON-RPC: dispatch each ``agent`` request as its own task so
    parallel()/pipeline() get real concurrency, gated by a semaphore(16)
  - enforce the 1000-agent lifetime cap and the USD budget BEFORE dispatch
  - consult the resume cache (instant, zero-cost hits) then run live via the
    injected agent runner (WS-C adapter in production, a fake in tests)
  - accumulate the token/cost ledger; journal every call; publish live events
  - cancel: tell the harness, cancel in-flight agents, kill the process group
"""

from __future__ import annotations

import asyncio
import logging
import os
import shutil

from server.workflows import rpc
from server.workflows.journal import Journal, call_hash

log = logging.getLogger("whisper-studio")

WORKFLOW_MAX_CONCURRENCY = 16
WORKFLOW_MAX_AGENTS = 1000
_LINE_LIMIT = 10 * 1024 * 1024

_HARNESS = os.path.join(os.path.dirname(__file__), "harness", "harness.mjs")


def _node_bin() -> str:
    return shutil.which("node") or "/usr/local/bin/node"


# --disallow-code-generation-from-strings makes eval()/new Function() throw in
# EVERY realm, which closes the leaked-host-object escape: a script can no longer
# do `agent.constructor.constructor("return process")()` to reach the host
# process / fs / child_process. vm.runInContext (the harness's own mechanism) is
# unaffected. This is the primary isolation layer alongside the absent globals,
# the scrubbed env, and the upfront user approval of every new script.
_NODE_HARDENING = ["--disallow-code-generation-from-strings"]


def _node_argv(*extra: str) -> list[str]:
    return [_node_bin(), *_NODE_HARDENING, *extra]


def _scrubbed_env() -> dict:
    # The harness has no network/fs need — hand it a minimal env with NO cloud
    # credentials, so even a hostile script cannot exfiltrate them.
    return {"PATH": "/usr/local/bin:/usr/bin:/bin:/sbin", "NODE_NO_WARNINGS": "1"}


def parse_workflow(source: str, *, timeout: float = 10) -> dict:
    """Synchronously run the harness in PARSE mode: syntax-check the script and
    extract+validate its ``meta`` literal WITHOUT executing it. Returns
    {name, description, phases}. Raises ValueError on any problem. Safe to call
    from a worker thread (used by the workflow_run tool for the approval
    preview)."""
    import json
    import subprocess

    start = rpc.dumps_line(rpc.control("start", {"mode": "parse", "source": source}))
    try:
        proc = subprocess.run(
            _node_argv(_HARNESS),
            input=start,
            capture_output=True,
            text=True,
            timeout=timeout,
            env=_scrubbed_env(),
        )
    except subprocess.TimeoutExpired as e:
        raise ValueError("workflow parse timed out") from e
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except (ValueError, TypeError):
            continue
        if msg.get("method") == "meta":
            return msg.get("params", {}).get("meta", {})
        if msg.get("method") == "fatal":
            raise ValueError(msg.get("params", {}).get("error", "parse failed"))
    raise ValueError("workflow script produced no meta (does it `export const meta = {...}`?)")


class WorkflowRun:
    def __init__(
        self,
        run_id: str,
        source: str,
        *,
        args=None,
        session_id: str = "",
        model_id: str = "",
        model_key: str = "",
        effort_label: str | None = None,
        budget_usd: float | None = None,
        depth: int = 0,
        journal: Journal | None = None,
        resume_cache: dict | None = None,
        agent_runner=None,
        nested_runner=None,
        on_event=None,
    ):
        self.run_id = run_id
        self.source = source
        self.args = args
        self.session_id = session_id
        self.model_id = model_id
        self.model_key = model_key or "sonnet"
        self.effort_label = effort_label
        self.budget_usd = budget_usd
        self.depth = depth
        self.journal = journal or Journal(run_id)
        self.resume_cache = resume_cache or {}
        self._agent_runner = agent_runner or self._default_agent_runner
        self._nested_runner = nested_runner
        self._on_event = on_event or (lambda ev: None)

        self.agents_spawned = 0
        self.tokens_in = 0
        self.tokens_out = 0
        self.cost_usd = 0.0
        self.cap_reached = False
        self._seq = 0
        self._sem = asyncio.Semaphore(WORKFLOW_MAX_CONCURRENCY)
        self._write_lock = asyncio.Lock()
        self._cancelled = False
        self.proc: asyncio.subprocess.Process | None = None

    async def _default_agent_runner(self, prompt, opts):
        from server.workflows.agent_adapter import run_workflow_agent

        return await run_workflow_agent(
            prompt,
            opts,
            session_id=self.session_id,
            default_model_id=self.model_id,
            effort_label=self.effort_label,
            run_id=self.run_id,
            depth=self.depth + 1,
        )

    # ── lifecycle ────────────────────────────────────────────────────────────

    async def run(self) -> dict:
        try:
            self.proc = await asyncio.create_subprocess_exec(
                *_node_argv(_HARNESS),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_scrubbed_env(),
                start_new_session=True,  # own process group for group-kill
                limit=_LINE_LIMIT,
            )
        except Exception as e:
            return self._result("failed", error=f"harness spawn failed: {e}")

        await self._send(
            rpc.control(
                "start",
                {
                    "mode": "run",
                    "source": self.source,
                    "run_id": self.run_id,
                    "args": {"value": self.args, "__budget_total__": self.budget_usd},
                },
            )
        )

        outcome = await self._pump()
        await self._reap()
        return outcome

    async def cancel(self) -> None:
        self._cancelled = True
        if self.proc and self.proc.returncode is None:
            try:
                await self._send(rpc.control("cancel"))
            except Exception:
                pass
            await asyncio.sleep(2)
            self._kill()

    # ── pump ─────────────────────────────────────────────────────────────────

    async def _pump(self) -> dict:
        tasks: set[asyncio.Task] = set()
        outcome: dict | None = None
        assert self.proc and self.proc.stdout
        try:
            while True:
                try:
                    line = await self.proc.stdout.readline()
                except (ValueError, asyncio.LimitOverrunError):
                    outcome = self._result("failed", error="harness produced an oversized line")
                    break
                if not line:
                    break
                try:
                    msg = rpc.loads_line(line)
                except rpc.RpcError:
                    continue
                if rpc.is_request(msg):
                    t = asyncio.create_task(self._handle_request(msg))
                    tasks.add(t)
                    t.add_done_callback(tasks.discard)
                elif rpc.is_notification(msg):
                    outcome = self._handle_notification(msg)
                    if outcome is not None:
                        break
        finally:
            for t in tasks:
                t.cancel()
        if outcome is None:
            outcome = self._result("failed", error="harness exited without a result")
        return outcome

    def _handle_notification(self, msg: dict) -> dict | None:
        method = msg.get("method")
        params = msg.get("params") or {}
        if method == "phase":
            self.journal.phase(params.get("name", ""))
            self._emit({"type": "phase", "name": params.get("name", "")})
        elif method == "log":
            self.journal.log(params.get("message", ""))
            self._emit({"type": "log", "message": params.get("message", "")})
        elif method == "done":
            self.journal.done(params.get("result"))
            return self._result("done", result=params.get("result"))
        elif method == "fatal":
            self.journal.error(params.get("error", ""), params.get("stack", ""))
            return self._result("failed", error=params.get("error", "workflow crashed"))
        return None

    async def _handle_request(self, msg: dict) -> None:
        method = msg.get("method")
        mid = msg.get("id")
        params = msg.get("params") or {}
        try:
            if method == "agent":
                await self._handle_agent(mid, params)
            elif method == "budget_spent":
                await self._respond(mid, {"spent": self.cost_usd})
            elif method == "workflow":
                await self._handle_nested(mid, params)
            else:
                await self._error(mid, rpc.ERR_INTERNAL, f"unknown method {method}")
        except Exception as e:  # noqa: BLE001
            await self._error(mid, rpc.ERR_INTERNAL, str(e))

    async def _handle_agent(self, mid, params: dict) -> None:
        prompt = params.get("prompt", "")
        opts = params.get("opts") or {}

        if self._cancelled:
            return await self._error(mid, rpc.ERR_CANCELLED, "run cancelled")

        # Caps/budget are checked BEFORE acquiring a slot so they fail fast.
        if self.agents_spawned >= WORKFLOW_MAX_AGENTS:
            self.cap_reached = True
            return await self._error(
                mid, rpc.ERR_AGENT_CAP, f"agent cap {WORKFLOW_MAX_AGENTS} reached"
            )
        if self.budget_usd is not None and self.cost_usd >= self.budget_usd:
            return await self._error(
                mid, rpc.ERR_BUDGET, f"budget ${self.budget_usd:.2f} exhausted"
            )

        self.agents_spawned += 1
        seq = self._seq
        self._seq += 1
        h = call_hash(prompt, opts)

        # Resume cache: an identical prior call replays instantly at zero cost.
        cached = self.resume_cache.get(h)
        if cached:
            result = cached.popleft()
            self.journal.agent_call(
                {
                    "seq": seq,
                    "call_hash": h,
                    "phase": opts.get("phase", ""),
                    "label": opts.get("label", ""),
                    "status": "cache_hit",
                    "text": result.get("text", ""),
                    "output": result.get("output"),
                    "usage": result.get("usage"),
                }
            )
            self._emit(
                {"type": "agent", "seq": seq, "status": "cache_hit", "label": opts.get("label", "")}
            )
            return await self._respond(mid, result)

        async with self._sem:
            if self._cancelled:
                return await self._error(mid, rpc.ERR_CANCELLED, "run cancelled")
            # Re-check the budget AFTER acquiring a slot: cost accrues only when
            # agents COMPLETE, so a burst of parallel() dispatches all pass the
            # pre-dispatch check at cost≈0. The semaphore serializes them to 16
            # at a time, and this re-check sees the cost accrued by earlier
            # completions — bounding overshoot to the concurrency limit instead
            # of the whole 1000-agent cap.
            if self.budget_usd is not None and self.cost_usd >= self.budget_usd:
                return await self._error(
                    mid, rpc.ERR_BUDGET, f"budget ${self.budget_usd:.2f} exhausted"
                )
            self._emit(
                {
                    "type": "agent",
                    "seq": seq,
                    "status": "running",
                    "label": opts.get("label", ""),
                    "phase": opts.get("phase", ""),
                }
            )
            result = await self._agent_runner(prompt, opts)

        # Price with the per-agent model when opts.model overrode the run's
        # model; opts.model is a config KEY (e.g. "sonnet"/"gpt5.6").
        self._account(result.get("usage") or {}, model_key=opts.get("model") or self.model_key)
        self.journal.agent_call(
            {
                "seq": seq,
                "call_hash": h,
                "phase": opts.get("phase", ""),
                "label": opts.get("label", ""),
                "status": result.get("status", "completed"),
                "text": result.get("text", ""),
                "output": result.get("output"),
                "usage": result.get("usage"),
                "agent_id": result.get("agent_id", ""),
            }
        )
        self._emit(
            {
                "type": "agent",
                "seq": seq,
                "status": result.get("status", "completed"),
                "label": opts.get("label", ""),
                "cost_usd": round(self.cost_usd, 4),
            }
        )
        await self._respond(mid, result)

    async def _handle_nested(self, mid, params: dict) -> None:
        if self.depth >= 1:
            return await self._error(mid, rpc.ERR_DEPTH, "nested workflow() is one level only")
        if not self._nested_runner:
            return await self._error(mid, rpc.ERR_INTERNAL, "nested workflows unavailable")
        if self.agents_spawned >= WORKFLOW_MAX_AGENTS:
            self.cap_reached = True
            return await self._error(mid, rpc.ERR_AGENT_CAP, "agent cap reached")
        # Pass THIS run so the child inherits the remaining budget/cap and its
        # spend is merged back — otherwise a nested workflow escapes the parent's
        # caps entirely.
        result = await self._nested_runner(params.get("name"), params.get("args"), self)
        await self._respond(mid, result)

    def absorb_child(self, child_outcome: dict) -> None:
        """Merge a nested run's spend/agents into this run so the parent's caps
        and ledger account for nested work."""
        self.agents_spawned += int(child_outcome.get("agents_spawned", 0) or 0)
        self.tokens_in += int(child_outcome.get("tokens_in", 0) or 0)
        self.tokens_out += int(child_outcome.get("tokens_out", 0) or 0)
        self.cost_usd += float(child_outcome.get("cost_usd", 0) or 0)
        if child_outcome.get("cap_reached"):
            self.cap_reached = True

    # ── ledger ───────────────────────────────────────────────────────────────

    def _account(self, usage: dict, model_key: str | None = None) -> None:
        ti = int(usage.get("input_tokens", 0) or 0)
        to = int(usage.get("output_tokens", 0) or 0)
        self.tokens_in += ti
        self.tokens_out += to
        try:
            from server.costs.tracker import estimate_cost

            self.cost_usd += estimate_cost(
                model_key or self.model_key,
                ti,
                to,
                cache_read_tokens=int(usage.get("cache_read", 0) or 0),
                cache_creation_tokens=int(usage.get("cache_creation", 0) or 0),
            )
        except Exception:
            pass

    # ── io ───────────────────────────────────────────────────────────────────

    async def _send(self, obj: dict) -> None:
        async with self._write_lock:
            if self.proc and self.proc.stdin and not self.proc.stdin.is_closing():
                self.proc.stdin.write(rpc.dumps_line(obj).encode("utf-8"))
                await self.proc.stdin.drain()

    async def _respond(self, mid, result) -> None:
        await self._send(rpc.response(mid, result))

    async def _error(self, mid, err_type: str, message: str) -> None:
        await self._send(rpc.error_response(mid, err_type, message))

    def _emit(self, ev: dict) -> None:
        try:
            self._on_event({"run_id": self.run_id, **ev})
        except Exception:
            pass

    def _result(self, status: str, *, result=None, error: str = "") -> dict:
        return {
            "status": status,
            "result": result,
            "error": error,
            "agents_spawned": self.agents_spawned,
            "tokens_in": self.tokens_in,
            "tokens_out": self.tokens_out,
            "cost_usd": round(self.cost_usd, 6),
            "cap_reached": self.cap_reached,
        }

    def _kill(self) -> None:
        if not self.proc or self.proc.returncode is not None:
            return
        # start_new_session=True made the harness its own process group leader
        # (pgid == pid), so one killpg reaps it and any children it spawned.
        import signal

        try:
            os.killpg(os.getpgid(self.proc.pid), signal.SIGKILL)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                self.proc.kill()
            except Exception:
                pass

    async def _reap(self) -> None:
        if not self.proc:
            return
        if self.proc.returncode is None:
            try:
                await asyncio.wait_for(self.proc.wait(), timeout=3)
            except (asyncio.TimeoutError, Exception):
                self._kill()
