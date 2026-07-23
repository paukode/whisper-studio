/* Architecture - the React SPA: shell, per-session runtimes, stores, SSE */
WSDiagram.mount("frontend-diagram", {
  title: "Frontend architecture",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 42 },
  groups: {
    browser: { label: "React SPA" }, transport: { label: "Transport" }
  },
  nodes: [
    { id: "shell", group: "browser", col: 0, row: 0.5, label: "AppShell", sub: "layout + init", desc: "Top-level layout (sidebar, chat, workspace, dock) plus the startup sequence: load config, models, retention, skills, MCP, restore sessions, populate the tool store, check workspace status." },
    { id: "runtimes", group: "browser", col: 1, row: 0, label: "Session runtimes", sub: "max 3 live", desc: "getRuntime() gives each session its own chat store, transcription store, cron EventSource, and abort controller. Up to MAX_LIVE_RUNTIMES = 3; idle hydrated sessions are evicted LRU." },
    { id: "stores", group: "browser", col: 2, row: 0.5, label: "Zustand stores", sub: "chat · ws · ui", desc: "Per-session factories (chat, transcription) plus singletons (session, workspace, ui, settings, layout, dock, recording, tool, task, cronUnread, subagent). Zustand v5: never return a fresh object from a selector." },
    { id: "stream", group: "browser", col: 1, row: 1, label: "useChatStream", sub: "SSE consumer", desc: "send() binds the owning session's chat store at call time, POSTs /api/chat, and never lets go on a session switch. One in-flight stream per session, up to MAX_ACTIVE_SESSIONS = 3 active at once." },
    { id: "sse", group: "transport", col: 2, row: 1.5, label: "readSSEStream", sub: "route by type", desc: "Parses the /api/chat SSE frames and dispatches each event (text, thinking, skill, tool_result, approval_request, team_progress, usage, grounding) into the owning session's stores." },
    { id: "san", group: "browser", col: 3, row: 0.5, label: "sanitizeHtml", sub: "marked + DOMPurify", desc: "renderMarkdownSafe: markdown -> marked.parse -> DOMPurify.sanitize before any raw-HTML render, so prompt-injected pages can't fire onerror payloads in the same-origin SPA." },
    { id: "dom", group: "browser", col: 4, row: 0.5, label: "React DOM", sub: "the visible UI", desc: "React 19 commits the active session's state to the DOM; a session switch re-points the binding in one commit." }
  ],
  edges: [
    { from: "shell", to: "runtimes", label: "create" },
    { from: "runtimes", to: "stores", label: "own" },
    { from: "shell", to: "stream", label: "send" },
    { from: "stream", to: "sse", label: "POST" },
    { from: "sse", to: "stores", label: "dispatch" },
    { from: "stores", to: "san", label: "markdown" },
    { from: "san", to: "dom", label: "safe HTML" }
  ]
});
