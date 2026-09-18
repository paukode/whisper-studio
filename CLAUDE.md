# Whisper Studio, development guide

Instructions for coding assistants and developers working on this repository.
The user-facing product docs live in `docs/`; the assistant's own prompt rules
live in `PROMPT_RULES.md`. This file is about how to change the code safely.

## What it is

A local, single-user AI workstation for macOS: a FastAPI backend (`server/`),
a React + TypeScript + Vite frontend (`src/`), a Swift app shell (`macapp/`),
and an on-device ASR stack. Cloud models run on Amazon Bedrock (Claude and
OpenAI on mantle); on-device chat runs through llama-server or MLX. One turn
engine (`server/chat/engine/runner.py`) serves chat, subagents, cron, and
headless runs.

## Invariants (review every change against these)

1. **Register always, refuse at execution.** The tool catalog is byte-stable
   across plan mode, strict-RAG, and workspace connect or disconnect. Per-turn
   restrictions are enforced in `server/tool_executor.py` and the executors,
   never by rewriting the tools array (that invalidates the prompt cache).
2. **Static before dynamic.** System prompt sections at or below
   `PromptLayer.WORKSPACE` are cached; anything that varies per request goes
   on `DYNAMIC` or later (`server/prompts/__init__.py`). Session-stable text
   parked on a dynamic layer is re-billed on every round.
3. **Reminders are persisted, not injected request-only.** Anything the model
   must see mid-turn is appended to the last user message
   (`server/chat/loop_hints.py::inject_reminder`), so the token prefix never
   forks between rounds.
4. **Every tool result flows through the safety gate.** Providers yield
   neutral events; `execute_tool_batch` and `process_tool_results` own hooks,
   permissions, approvals, budgeting, and loop guards. Adapters never read
   tool output.
5. **SSE frames are pinned.** `tests/golden_fixtures` pins the frame
   vocabulary (`GOLDEN_RECORD=1` re-records on purpose). The frontend Zod
   schema (`src/types/schemas/chat.schema.ts`) strips unknown fields, so a
   new frame or field must be added there and in `src/types/chat.ts`.
6. **Import boundaries.** `server/infrastructure/config.py` never imports
   through `server.chat`. `server/tool_router.py` is pure dispatch; lifecycle
   lives in `tool_executor.py`.
7. **One deliverable, one file.** File-producing tools overwrite the same path
   atomically; never create `_v2` or `_final` strays.
8. **User files live in `~/.whisper`**, app storage under the data root.
   Tests pin `WHISPER_USER_DIR` and redirect every import-time DB path
   (`tests/conftest.py::_DB_MODULES`). A new sqlite table needs a numbered
   migration in `server/migrations/`, the same schema in the module's own
   `_ensure_*` bootstrap, and its module listed in `_DB_MODULES`.
9. **File-size budget.** CI fails any `server/` or `src/` file over 1200 lines
   (`tests/test_file_size_budget.py`). Split along a topic, do not allowlist.
10. **No em dashes, no emoji** in code comments, prompts, docs, or commit
    messages. Use a comma, colon, period, or parentheses.

## Layout

```
server/chat/engine/      runner.py (the loop), anthropic.py, openai.py, local.py
server/chat/             routes.py (turn assembly), compaction.py, tool_pool.py,
                         tool_partition.py (core set), loop_hints.py, loop_guard.py
server/tool_executor.py  lifecycle: hooks, permissions, plan mode, loop guard
server/tool_router.py    name to handler dispatch
server/goals/            completion gate: Stop hooks, deliverables, verification, gates, evaluator
server/memory/           two-tier memory, extraction, dream consolidation, review fork
server/agents/           subagent runtime, agent types, tool filtering, journal.py
                         (on-disk record of every run: events, message checkpoints,
                         report; the task registry row exists from the first round)
server/infrastructure/   config, sessions store, session_search, auxiliary models, one_shot
server/skills.py         skill loader; skills_routes.py (HTTP); agent_tools/ (model tools)
src/hooks/useSlashCommands.tsx   slash commands; src/types/schemas/chat.schema.ts SSE schema
macapp/                  build_app.sh then make_dmg.sh (packages dist-app/)
```

## Development

```bash
bash setup.sh            # provisions venv/ and the frontend bundle
bash setup.sh --dev      # Vite dev server with HMR; the backend serves /static CSS
venv/bin/python -m pytest tests/        # backend suite (default env, no extra vars)
venv/bin/ruff check . && venv/bin/ruff format --check .
npx tsc --noEmit && npm test && npm run lint && npm run build
```

- Only Node 24 is installed locally; do not assume other versions.
- Run the full pytest suite with the DEFAULT environment. Two local-model
  resolver tests are known red on some Macs regardless of code; gate on the
  exit code, and never trust a piped `pytest | tail` exit status.
- The live runtime config is the gitignored `config.json` at the repo root,
  merged over `config.example.json` and `DEFAULTS`.
- Several sessions may share this checkout and hold ports. Never kill
  processes by name; find the owner by port with `lsof -i :PORT`. Ship from a
  scratchpad worktree, never by switching branches in a shared tree.
- Destructive shell commands take absolute paths and a same-call `cd`; the
  working directory resets between tool calls.

## Testing conventions

- Behaviour contracts over snapshots: assert how two pieces of data relate,
  never freeze a catalog count or a model list.
- Patch where production reads. Many modules import lazily inside functions;
  monkeypatch the attribute the call site actually resolves.
- Mock `load_config` with the NORMALIZED shape (rich `chat_models` entries,
  `chat_model_meta` present), not the raw example JSON.
- New SSE fields need a golden refresh and a Zod schema entry, or the UI
  silently drops them.

## Shipping

Branch, granular commits, PR, merge when LOCAL checks are green, fast-forward
local main, delete branches. Commit messages are short and precise, never
mention an AI assistant, and never end with a dash. Do not wait for GitHub CI
as a merge gate; verify locally. After a merge that touches the app, rebuild
the DMG from the same main commit so `setup.sh` installs and the Mac app stay
in parity (the DMG is named by `git describe` but packages `dist-app/`; verify
the bundle contents, not the filename).
