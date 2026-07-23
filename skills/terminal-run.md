---
name: terminal_run
description: Runs a one-shot, non-interactive shell command and returns the exit code plus combined stdout/stderr. mode=sandbox (default) uses a hidden ephemeral PTY with a minimal rc-free shell, sandboxed away from secrets; use it to check, try, or probe. mode=visible types the command into the user's open terminal so they can watch, and errors if none is open; use it only when the user asks to see it run. Every call is approval-gated and validated first; dangerous commands and interactive ones that wait for stdin (vim, less, top, watch, ssh without BatchMode=yes, sudo without -n) are refused before running. Default timeout 30s, max 300s; on timeout partial output is returned. Works without a connected workspace, but prefer ws_run_command for project commands when a workspace is connected. Not for long-running servers or watch loops.
triggers: shell, command, terminal, bash, zsh, cli, exec, install, npm, pip, brew, sandbox
executor: terminal_run
input_schema:
  command:
    type: string
    required: true
    description: The shell command to execute, single line; compose with && ; or pipes. Must be non-interactive.
  mode:
    type: string
    description: Either sandbox (default; hidden ephemeral PTY, invisible to the user) or visible (types into the user's open terminal and errors if none is open).
  timeout:
    type: number
    description: Seconds before the command is killed. Default 30, max 300.
  cwd:
    type: string
    description: Working directory. Defaults to the workspace path if connected, else $HOME. Supports ~ expansion; a nonexistent path falls back silently.
---

Runs a shell command after user approval; the approval card shows the literal command
and mode. Commands are validated first: dangerous patterns are refused, and so are
interactive commands that would wait for stdin forever (vim, less, top, watch, ssh
without BatchMode=yes, sudo without -n).

Sandbox mode spawns a hidden ephemeral PTY with a minimal shell (bash --noprofile
--norc, falling back to zsh -f) so output is not polluted by prompt themes or rc
files; on macOS it is additionally wrapped in sandbox-exec away from secret paths.
Visible mode uses the user's actual open terminal and login shell so they can watch
the command stream.

The result is the exit code, a separator, and the ANSI-stripped output. Timed-out
runs return the partial output captured so far.
