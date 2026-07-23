---
name: verify_change
description: Runs the repository's full verification gate (ruff, pytest, tsc + vite build, vitest, eslint) and, for UI-facing changes, drives the live browser preview, then reports a single deterministic PASS/FAIL verdict. Use before claiming a code change is done, when a goal or completion gate needs proof the work actually holds, or when the user asks to "verify", "run the checks", "make sure it works", or "check CI". The final line is exactly "VERIFY PASS" or "VERIFY FAIL: <reason>" so the goal evaluator and Stop hooks can key on it.
triggers: verify, verify change, run the checks, run the gate, check CI, make sure it works, does it build, run the tests, confirm it works, is it passing
input_schema:
  scope:
    type: string
    required: false
    description: Optional path or area to focus the checks on (e.g. "server/goals" or "the hooks panel"). When omitted, verify the whole change set.
  ui_facing:
    type: boolean
    required: false
    description: Set true when the change affects rendered UI so the browser preview is exercised in addition to the CI gate.
---

# Verify a change

Prove the current change actually holds by running the repository's verification
gate yourself, in order, and reporting one deterministic verdict. Do not claim
success from reading the diff — run the commands and read their output.

## The gate (run in this order, stop reasoning about later steps only if an earlier one hard-fails to even start)

Run each with the workspace shell tools (`ws_run_command` / background shell).
Use the project's virtualenv for Python tools when one exists (`venv/bin/ruff`,
`venv/bin/python -m pytest`).

1. **Lint + format (Python):** `ruff check .` and `ruff format --check .`
2. **Backend tests:** `python -m pytest -q` — isolate data with a fresh
   `WHISPER_DATA_DIR` (`WHISPER_DATA_DIR=$(mktemp -d) python -m pytest -q`) so
   the run never touches real app data. If a known-slow suite hangs locally,
   note it and scope pytest to the changed area rather than silently skipping.
3. **Frontend build (types + bundle):** `npm run build` (this is `tsc -b` then
   `vite build`).
4. **Frontend unit tests + lint:** `npm test` (vitest `--run`) and
   `npm run lint` (eslint `src/`).

When `scope` is given, you may narrow steps 2 and 4 to that area for speed, but
lint/format and the build always run whole — they are cheap and catch drift.

## UI-facing changes (`ui_facing: true`)

Additionally drive the live preview to prove the UI renders and behaves:

1. Start (or reuse) the dev server via the preview tools; if `preview_list`
   shows nothing and `preview_start` is unavailable, note that preview tooling
   is off and continue with the CI gate only.
2. Load the relevant view, then check `read_console_messages` for errors,
   `read_page` for the expected content/structure, and drive any changed
   interaction (`computer` click/type, `form_input`) followed by a re-read to
   confirm the result.
3. Capture a screenshot as evidence for the summary.

## Report

Summarize what you ran and the outcome of each step in a few lines (command →
pass/fail, with the first real error line on any failure). Then end with EXACTLY
one of these as the final line, nothing after it:

- `VERIFY PASS` — every gate step succeeded (and, for UI changes, the preview
  rendered without console errors and the changed behavior worked).
- `VERIFY FAIL: <one-line reason>` — the first blocking failure, named
  concretely (which command, which test, which error).

Never emit both. Never soften a real failure into a pass. A flaky or
environment-only failure that you could not resolve is still a FAIL with the
reason stated — the evaluator weighs this token above any prose claim of "done".
