# Memory System

Two-tier persistent memory for Whisper Studio. Stores facts, preferences, and project context across sessions so the assistant remembers what matters.

| Tier | Location | Alive when | Holds |
|---|---|---|---|
| **global** | `data/global_memory/` | always, including plain chat with no workspace | user preferences, role, cross-project feedback |
| **project** | `data/memory/<workspace-slug>/` | a workspace is open | goals, deadlines, repo-specific references |

## Quick Start

1. **Memory is on by default** (`auto_memory` feature flag). Toggle it from the "+ More" menu in the chat toolbar (the "Memory" row), or type `/memory on|off`.

2. **Chat normally.** No workspace needed: global memories extract and recall in plain chat mode. Open a workspace to add the project tier on top.

---

## How It Works

### Memory Lifecycle

```
User sends message
       |
       v
  [RECALL] Scan both tiers -> Haiku selects relevant files -> injected into prompt
       |
       v
  Assistant responds (with memory context available)
       |
       v
  [EXTRACT] Background agent analyzes conversation -> writes new memory files,
            routing each to global or project via the scope parameter
       |
       v
  [INDEX] MEMORY.md regenerates automatically after every write/delete
       |
       v
  [DREAM] Per tier, after 24h + 5 turn-ends: consolidation agent merges,
          updates, and prunes topic files
```

### Storage Layout

```
data/global_memory/             # Global tier (cross-workspace)
  MEMORY.md                     # Auto-generated index (do not edit by hand)
  user_role.md                  # Topic files with YAML frontmatter
  feedback_style.md
  .cursor.json                  # Extraction cursors + turn counters, keyed by session id
  .dream_meta.json              # Dream consolidation metadata

data/memory/
  myproject/                    # Project tier (slug = workspace folder name)
    MEMORY.md                   # Auto-generated index
    project_goals.md
    reference_dashboards.md
    .dream_meta.json

data/session_memory/
  <session-id>.md               # Per-session structured summaries
```

`data/global_memory/` deliberately lives outside `data/memory/` so it can never collide with a workspace slug.

### Topic File Format

Every memory file uses YAML frontmatter:

```markdown
---
name: User Role
description: Backend engineer working on payments, prefers concise answers
type: user
---

- Role: Backend engineer on the payments service
- Prefers concise, direct answers
```

**Required fields:** `name`, `description` (used by recall to judge relevance), `type`.

### Memory Types and Default Scope Routing

| Type | What to store | Default tier |
|---|---|---|
| `user` | Role, preferences, expertise | global |
| `feedback` | Corrections and guidance on approach | global |
| `project` | Goals, deadlines, context not in code | project (global if no workspace) |
| `reference` | Pointers to external systems | project (global if no workspace) |

An explicit `scope` parameter on any memory tool overrides the default. Unscoped writes to a file that already exists somewhere update it in place, whatever its tier. Unscoped reads/deletes search project first, then global.

### MEMORY.md Index

Auto-generated from topic-file frontmatter after every `memory_write` / `memory_delete` (and after dream consolidation). One pointer line per file, newest first:

```markdown
- [user] [User Role](user_role.md): Backend engineer, payments, prefers concise answers
- [project] [Q2 Goals](project_goals.md): Payment processing latency target under 200ms
```

Direct writes to MEMORY.md through the memory tools are blocked (case-insensitively; APFS would otherwise let `memory.md` overwrite it). **Limits when loaded:** 200 lines, 25 KB; a warning is appended if truncated.

---

## User Interaction

### Automatic (no action needed)

When `auto_memory` is enabled:

- **Recall:** Before each response, both tiers are scanned and relevant files are injected into the prompt. With more than 5 files total, Haiku picks the most relevant (on-device models skip the cloud side-query and take the most recent instead).
- **Extraction:** After every 3rd chat turn, a background agent reviews the conversation and saves new memories. Runs with or without a workspace; skips if the assistant already wrote memory that turn, or if a previous extraction for the session is still running.

### Direct commands (ask the assistant in chat)

| What to say | What happens |
|---|---|
| "Remember that I prefer TypeScript" | `memory_write` (routes to global: type user) |
| "Remember for this project that we ship on the 15th" | `memory_write` with scope project |
| "List all my memories" | `memory_list` shows both tiers |
| "Read the memory about testing" | `memory_read` (searches project, then global) |
| "Forget that I use VS Code" | finds and deletes the relevant file |

### Verifying memories on disk

- Plain chat: look in `data/global_memory/` for `.md` files.
- Workspace open: also `data/memory/<workspace-slug>/`.
- Every folder with topic files has an auto-generated `MEMORY.md` index.

---

## Feature Flags

All three memory flags default **ON** (overridable in `config.json` under `feature_flags`).

| Flag | Default | What it gates |
|---|---|---|
| `auto_memory` | on | Both tiers: recall, extraction, memory tools |
| `session_memory` | on | Per-session structured summaries |
| `dream_consolidation` | on | Per-tier consolidation agent |

Secret scanning is not a flag: it always runs on every memory write and cannot be disabled.

---

## Secret Scanning

Memory writes are scanned for 35+ secret patterns before saving to disk (AWS tokens, GitHub PATs, API keys for Anthropic/OpenAI/HuggingFace, Slack tokens, private key headers, Stripe keys, and more). If a secret is detected the write is **blocked**; the content is never persisted. This applies to both tiers.

---

## Session Memory

When `session_memory` is enabled, each session maintains a structured summary (Goals / Decisions / Context / Blockers) at `data/session_memory/<session-id>.md`, injected into the system prompt on later turns of the same session.

**Triggers:** conversation exceeds ~50k tokens AND 10+ tool calls. Sections capped at 2,000 characters, 12,000 total.

---

## Dream Consolidation

When `dream_consolidation` is enabled, each tier is consolidated independently once **both** hold:
- 24+ hours since that tier's last consolidation
- 5+ turn-ends recorded since then

The check runs after every turn (cheap file reads); when due, a 4-phase agent (Orient, Gather, Consolidate, Prune) merges duplicates, updates stale facts, and deletes superseded topic files for that tier only. The MEMORY.md index regenerates automatically as it works.

**Status:** `data/global_memory/.dream_meta.json` and `data/memory/<slug>/.dream_meta.json` hold `last_consolidated_at` and `session_count`. A PID-based lock file (`.dream.lock`) prevents concurrent consolidations (1-hour stale timeout).

---

## Architecture

```
server/memory/
  __init__.py          # Public API: init_memory(), MEMORY_TOOLS, MEMORY_TOOL_NAMES
  memdir.py            # Tier/scope resolution, MEMORY.md load + rebuild_index()
  scan.py              # File scanning, YAML frontmatter parsing, manifest building
  recall.py            # recall_memory_context(): two-tier scan + Haiku selection
  extract.py           # Post-turn background extraction (session-keyed cursors)
  session_memory.py    # Per-session structured summaries
  secret_scanner.py    # 35+ regex rules for secret detection
  dream.py             # record_and_maybe_dream(): per-tier consolidation
  tools.py             # Bedrock tool schemas (memory_read/write/list/delete, scope param)
  executor.py          # Tool implementations (scope routing, atomic writes)
  prompts.py           # System prompts for extraction, recall, consolidation
```

### Integration points in the codebase

| File | What it does |
|---|---|
| `chat/routes.py` | Recall before prompt build, extraction + dream after end_turn, tools in pool |
| `openai_bedrock/stream.py` | Same post-turn hooks for GPT-5.x turns |
| `prompts/__init__.py` | memory_context and session_memory_context prompt sections |
| `tool_router.py` | Memory tool dispatch |
| `agents/config.py` | memory_extractor agent type (uses prompts.EXTRACTION_SYSTEM_PROMPT) |
| `infrastructure/feature_flags.py` | 3 flag registrations |
| `infrastructure/sessions.py` | Drops per-session extraction state on session delete |
| `main.py` | Init, executor import |

---

## Troubleshooting

**Memories not being created:**
- Check the Memory row in the "+ More" toolbar menu is On, or type `/memory` for status
- Extraction triggers every 3rd turn per session — send at least 3 messages
- Check the server terminal for `Memory extraction` log entries
- Plain chat writes go to `data/global_memory/`, not the project folder

**Memories not recalled:**
- Check the tier folders for `.md` files with valid frontmatter
- `MEMORY.md` regenerates on the next write/delete; its absence just means no topic files
- Server logs show warnings on recall failure

**Secret scanner blocking writes:**
- This is intentional — remove the secret from the content
- Check server terminal for the specific rule that fired

**Dream not running:**
- Needs both 24h elapsed AND 5 recorded turn-ends for that tier
- Check the tier's `.dream_meta.json` for current state

**No `data/global_memory/` folder:**
- Created at startup by `init_memory()` and on first use; restart the server and check again
