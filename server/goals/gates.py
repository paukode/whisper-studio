"""Goal quality gates: shell commands that must exit 0 before a goal can be
judged done.

The evaluator is an LLM reading prose; a gate is stronger. ``/goal gate add
<command>`` stores the command on the session's goal state. At every turn
boundary while the goal is active, the gates run BEFORE the evaluator: a red
gate is deterministic proof the goal is not met, its exit code and output tail
become the continuation feedback, and the evaluator is not called at all.
Every boundary re-runs a failed gate against the current tree, so a gate whose
input the agent just repaired passes on the next boundary. Retries are bounded
per gate (default 3); an exhausted gate pauses the goal instead of looping.

A gate runs under the macOS sandbox like a confined terminal_run command:
writes are confined to the workspace (plus the session's scratch folder and
temp), the credential deny-list applies, and without sandbox-exec the gate is
refused and nothing runs.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass

from server.sandbox import SandboxUnavailableError, run_sandboxed

GATE_MAX_RETRIES = 3
GATE_TIMEOUT_SECONDS = 300
_TAIL_CHARS = 3000


@dataclass
class GateResult:
    command: str
    passed: bool
    exit_code: int | None
    tail: str


def _tail(text: str) -> str:
    text = text or ""
    return text if len(text) <= _TAIL_CHARS else "..." + text[-_TAIL_CHARS:]


def run_gate(
    command: str,
    workspace: str | None,
    *,
    timeout: int = GATE_TIMEOUT_SECONDS,
    scratch_dir: str | None = None,
) -> GateResult:
    """Run one gate command in the workspace under the sandbox. Never raises.

    Writes are confined to the workspace, ``scratch_dir`` and temp. Without a
    workspace the gate starts in $HOME and writes no tree. When sandbox-exec
    cannot be used the gate is refused: a failed result with the reason."""
    ws = workspace if workspace and os.path.isdir(workspace) else None
    try:
        proc = run_sandboxed(
            command,
            cwd=ws or os.path.expanduser("~"),
            timeout=timeout,
            write_mode="workspace",
            write_root=ws,
            scratch_dir=scratch_dir,
        )
    except SandboxUnavailableError as e:
        return GateResult(command, False, None, str(e))
    except subprocess.TimeoutExpired:
        return GateResult(command, False, None, f"timed out after {timeout}s")
    except Exception as e:  # noqa: BLE001
        return GateResult(command, False, None, f"could not run: {e}")
    out = (proc.stdout or "") + (("\n" + proc.stderr) if proc.stderr else "")
    return GateResult(command, proc.returncode == 0, proc.returncode, _tail(out.strip()))


def run_gates(
    commands: list[str], workspace: str | None, scratch_dir: str | None = None
) -> list[GateResult]:
    return [run_gate(c, workspace, scratch_dir=scratch_dir) for c in commands if (c or "").strip()]


def format_failure(results: list[GateResult]) -> str:
    """Continuation feedback for the first failing gate(s)."""
    failed = [r for r in results if not r.passed]
    parts: list[str] = []
    for r in failed[:2]:
        code = "no exit code" if r.exit_code is None else f"exit code {r.exit_code}"
        parts.append(f"quality gate `{r.command}` failed ({code}).\nOutput tail:\n{r.tail}")
    more = f"\n({len(failed) - 2} more gates also failed.)" if len(failed) > 2 else ""
    return (
        "[gate] "
        + "\n\n".join(parts)
        + more
        + "\n\nFix the underlying problem so this command exits 0, then continue toward the "
        "goal. The gate re-runs at the next turn boundary."
    )


__all__ = [
    "GATE_MAX_RETRIES",
    "GATE_TIMEOUT_SECONDS",
    "GateResult",
    "format_failure",
    "run_gate",
    "run_gates",
]
