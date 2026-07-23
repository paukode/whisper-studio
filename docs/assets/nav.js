/* ================================================================
   Whisper Studio Docs - navigation model (single source of truth)
   Consumed by site.js to build the sidebar, breadcrumb, prev/next,
   and the client-side search index. Classic script (no modules).
   ================================================================ */
window.WS_NAV = [
  {
    title: "Overview",
    items: [
      { t: "Introduction", h: "index.html", d: "What Whisper Studio is and how to read these docs." }
    ]
  },
  {
    title: "Get started",
    items: [
      { t: "Requirements", h: "requirements.html", d: "OS, Python, Homebrew, AWS, hardware for model modes." },
      { t: "Installation", h: "installation.html", d: "Zero-to-running walk-through, setup.sh flags, model modes." },
      { t: "Configuration", h: "configuration.html", d: "Config layers, environment variables, Settings UI." },
      { t: "First run & verify", h: "first-run.html", d: "Connect AWS, pick a model, first chat and recording." }
    ]
  },
  {
    title: "Tutorials",
    items: [
      { t: "Your first chat", h: "tut-first-chat.html", d: "Models, effort, slash commands, streaming, export." },
      { t: "Goals & autopilot", h: "tut-goals.html", d: "Prompt vs a goal that runs to completion vs a parallel workflow — and how to set a goal." },
      { t: "Voice & meetings", h: "tut-voice.html", d: "Dictation, diarization, speaker rename, system audio." },
      { t: "Working with documents", h: "tut-documents.html", d: "Drag-drop attachments, extraction, follow-ups." },
      { t: "Web research", h: "tut-research.html", d: "Tavily web search & fetch, citations." },
      { t: "The workspace IDE", h: "tut-workspace.html", d: "File tree, Monaco, terminal, Git, LSP, search." },
      { t: "Permissions & approvals", h: "tut-permissions.html", d: "The Mode dial and approval cards." },
      { t: "Memory & WHISPER.md", h: "tut-memory.html", d: "Cross-session memory and project context." },
      { t: "Skills", h: "tut-skills.html", d: "Built-in and custom Markdown skills." },
      { t: "Sub-agents & teams", h: "tut-subagents.html", d: "Spawn, coordinate, and watch parallel agents." },
      { t: "Scheduled tasks (cron)", h: "tut-cron.html", d: "Create jobs, run history, inline result cards." },
      { t: "Index & semantic search", h: "tut-index-search.html", d: "Index a folder, entity graph, retrieval grounding." },
      { t: "MCP & plugins", h: "tut-mcp-plugins.html", d: "Connect external tools; opt-in Python plugins." },
      { t: "Model modes", h: "tut-model-modes.html", d: "Cloud vs hybrid vs local; per-capability backends." }
    ]
  },
  {
    title: "Architecture",
    items: [
      { t: "Overall system", h: "overall-system.html", d: "The whole architecture on one page: system map + chat-turn sequence." },
      { t: "System overview", h: "arch-overview.html", d: "The whole system at a glance - interactive map." },
      { t: "Anatomy of a chat turn", h: "arch-chat-turn.html", d: "End-to-end sequence of one request." },
      { t: "Chat & tool orchestration", h: "arch-chat-pipeline.html", d: "The core streaming state machine and tool loop." },
      { t: "Tool dispatch & executors", h: "arch-tools.html", d: "Routing, concurrent-safe batching, hooks." },
      { t: "Sub-agents & the event bus", h: "arch-agents.html", d: "Agent runtime, teams, per-session pub/sub." },
      { t: "Ultracode workflow runtime", h: "arch-ultracode.html", d: "Model-authored JS workflows in a Node vm harness; agent(), budgets, resume, CI autofix." },
      { t: "Blocking hooks", h: "arch-hooks.html", d: "PreToolUse deny/rewrite, PostToolUse context, and the Stop gate across every path." },
      { t: "Goal loop & completion gate", h: "arch-goals.html", d: "Set a goal; an end-of-turn gate runs Stop hooks + a cheap evaluator until it's met." },
      { t: "Approval & permissions", h: "arch-approval.html", d: "Nonce flow, permission modes, auto-mode classifier." },
      { t: "Sandbox & security", h: "arch-sandbox.html", d: "sandbox-exec/bwrap, validator, deny lists, boundaries." },
      { t: "Transcription & diarization", h: "arch-transcription.html", d: "WebSocket audio, Whisper/Parakeet, speaker ID." },
      { t: "Transcript summarization", h: "arch-summarization.html", d: "Map-reduce condensation of huge transcripts; the summary skills." },
      { t: "Attachment extraction", h: "arch-extraction.html", d: "Type-aware file to Markdown, OCR, vision sizing." },
      { t: "Index / GraphRAG & search", h: "arch-index.html", d: "Embeddings, reranker, entities, sqlite-vec, grounding." },
      { t: "Memory system", h: "arch-memory.html", d: "Session + workspace memory and prompt injection." },
      { t: "Cron / scheduled tasks", h: "arch-cron.html", d: "Wall-clock scheduling, run history, cron events." },
      { t: "Model backends & modes", h: "arch-backends.html", d: "Anthropic / OpenAI-on-Bedrock / local Gemma." },
      { t: "Live preview & browser", h: "arch-preview.html", d: "Dev-server spawn, Playwright, screenshots." },
      { t: "Cost tracking & budgets", h: "arch-costs.html", d: "Per-turn logging, forecasts, spend caps." },
      { t: "Git integration & LSP", h: "arch-git-lsp.html", d: "Git wrappers + watcher, LSP proxy." },
      { t: "Persistence", h: "arch-persistence.html", d: "SQLite (WAL) schema, session latch, migrations." },
      { t: "Frontend architecture", h: "arch-frontend.html", d: "SPA shell, runtimes, Zustand stores, SSE hooks." }
    ]
  },
  {
    title: "Reference",
    items: [
      { t: "Slash commands", h: "ref-slash-commands.html", d: "Every / command in the chat input." },
      { t: "Agent tools", h: "ref-tools.html", d: "The full tool pool the assistant can call, grouped." },
      { t: "Environment variables", h: "ref-env.html", d: "HOST, PORT, TAVILY_API_KEY, and friends." },
      { t: "Settings & config keys", h: "ref-settings.html", d: "Every Settings-UI field and config.json key." },
      { t: "Keyboard shortcuts", h: "ref-shortcuts.html", d: "Keys for chat, recording, and the workspace." },
      { t: "Security model", h: "ref-security.html", d: "Boundaries vs defence-in-depth, at a glance." },
      { t: "Glossary", h: "ref-glossary.html", d: "Terms used across Whisper Studio." }
    ]
  },
  {
    title: "Contribute",
    items: [
      { t: "Development & contributing", h: "contributing.html", d: "Run tests, project layout, conventions, tech stack." }
    ]
  }
];
