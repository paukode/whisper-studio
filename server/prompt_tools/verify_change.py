"""verify_change: the project's own checks, run in order, as a native prompt tool.

The recipe names no fixed commands. It once listed this repository's own gate
(ruff, pytest, tsc, vitest, eslint), and a session at work in an unrelated
project was sent to run them there.
"""

from server.executors import register_executor
from server.prompt_tools._render import render_prompt

TOOL = {
    "name": "verify_change",
    "description": (
        "Runs the checks the current project itself defines (its own lint, typecheck, "
        "build and test commands, found in its files) and, for UI-facing changes, drives "
        "the live browser preview, then reports a single deterministic PASS/FAIL verdict. "
        "For a project with no checks of its own, it runs what was built and reports "
        "that. Use before "
        "claiming a code change is done, when a goal or completion gate needs proof "
        'the work actually holds, or when the user asks to "verify", "run the '
        'checks", "make sure it works", or "check CI". The final line is exactly '
        '"VERIFY PASS" or "VERIFY FAIL: <reason>" so the goal evaluator and Stop '
        "hooks can key on it."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "scope": {
                "type": "string",
                "description": (
                    "Optional path or area to focus the checks on (e.g. "
                    '"server/goals" or "the hooks panel"). When omitted, verify the '
                    "whole change set."
                ),
            },
            "ui_facing": {
                "type": "boolean",
                "description": (
                    "Set true when the change affects rendered UI so the browser "
                    "preview is exercised in addition to the CI gate."
                ),
            },
        },
        "required": [],
    },
}

BODY = r"""# Verify a change

Prove the current change holds by running the project's own checks yourself,
in order, and reporting one deterministic verdict. Do not claim success from
reading the diff: run the commands and read their output.

## Find the checks

The project decides what its checks are. Read them from its files, do not
assume them:

- its instructions for contributors or assistants (README, CONTRIBUTING, WHISPER.md, CLAUDE.md, AGENTS.md)
- its task definitions (`package.json` scripts, `Makefile`, `pyproject.toml`, `Cargo.toml`, `go.mod`, a CI workflow file)

Run only checks the project defines. Do not add a linter, a formatter or a test
framework the project does not use, and do not write new tests to have something
to run.

## Run them (in this order, stop reasoning about later steps only if an earlier one hard-fails to even start)

Run each with the workspace shell tools (`ws_run_command` / background shell).
Use the project's own environment when one exists (a virtualenv, the pinned
Node version).

1. **Lint and format checks**, if the project defines them.
2. **Typecheck and build**, if the project defines them.
3. **Tests**, if the project defines them. If a slow suite hangs locally, note
   it and scope the tests to the changed area rather than silently skipping.

When `scope` is given, you may narrow the tests to that area for speed, but
lint and the build run whole: they are cheap and catch drift.

## A project with no checks of its own

A script, a generated file or a one-off deliverable often has none. Then the
check is to run what was built: run the script and read its exit status and
output, or open the file it produced. Report exactly that. Do not invent a
suite for it.

## UI-facing changes (`ui_facing: true`)

Additionally drive the live preview to prove the UI renders and behaves:

1. Start (or reuse) the dev server via the preview tools. If a preview tool
   says the browser is missing, note it and continue with the project's checks only.
2. Load the relevant view, then check `preview_console_logs` for errors,
   `preview_snapshot` (or `preview_inspect`) for the expected
   content/structure, and drive any changed interaction (`preview_click`,
   `preview_fill`) followed by a re-read to confirm the result.
3. Capture a screenshot with `preview_screenshot` as evidence for the summary.

## Report

Summarize what you ran and the outcome of each step in a few lines (command,
pass or fail, with the first real error line on any failure). Then end with EXACTLY
one of these as the final line, nothing after it:

- `VERIFY PASS`: every check succeeded (and, for UI changes, the preview
  rendered without console errors and the changed behavior worked).
- `VERIFY FAIL: <one-line reason>`: the first blocking failure, named
  concretely (which command, which test, which error).

Never emit both. Never soften a real failure into a pass. A flaky or
environment-only failure that you could not resolve is still a FAIL with the
reason stated: the evaluator weighs this token above any prose claim of "done"."""


@register_executor("verify_change", read_only=True, concurrent_safe=True, emits_prompt=True)
def exec_verify_change(tool_input, transcript, current_attachments):
    return render_prompt(tool_input, BODY)
