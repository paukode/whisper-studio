/* Architecture - full system overview (interactive). The complete whole-system
   map from the overall-system architecture page: individual FastAPI routers, executors, and
   persistence stores. Click a box to trace its downstream path. */
WSDiagram.mount("overview-diagram", {
  title: "Whisper Studio system overview",
  grid: { nodeW: 150, nodeH: 58, gapX: 42, gapY: 34 },
  groups: {
    browser: { label: "Browser" }, transport: { label: "Transport" },
    server: { label: "Routers" }, tools: { label: "Tools" }, agents: { label: "Agents" },
    security: { label: "Security" }, local: { label: "On-device" }, index: { label: "Index" },
    persist: { label: "Storage" }, external: { label: "Cloud" }
  },
  zones: [
    { label: "Browser (React SPA)", group: "browser", col: 0, row: 0, cols: 1, rows: 3 },
    { label: "Same origin", group: "transport", col: 1, row: 0, cols: 1, rows: 3 },
    { label: "FastAPI routers", group: "server", col: 2, row: 0, cols: 2, rows: 3 },
    { label: "Tool loop", group: "tools", col: 4, row: 0, cols: 1, rows: 4 },
    { label: "Executors", group: "tools", col: 5, row: 0, cols: 1, rows: 5 },
    { label: "Security", group: "security", col: 6, row: 0, cols: 1, rows: 3 },
    { label: "Local runtime", group: "local", col: 6, row: 3, cols: 1, rows: 4 },
    { label: "Persistence", group: "persist", col: 7, row: 0, cols: 1, rows: 4 },
    { label: "External", group: "external", col: 7, row: 4, cols: 1, rows: 3 }
  ],
  nodes: [
    { id: "spa", group: "browser", col: 0, row: 0, label: "React 19 SPA", sub: "components + hooks", desc: "Chat, live transcript, and the workspace IDE. Streams over hooks like useChatStream and useChatInputMic." },
    { id: "stores", group: "browser", col: 0, row: 1, label: "Zustand stores", sub: "per-session runtimes", desc: "Client state: chat, session, workspace, recording, settings. Up to three sessions stream at once." },
    { id: "sanitize", group: "browser", col: 0, row: 2, label: "sanitizeHtml", sub: "marked + DOMPurify", desc: "Every Markdown render is sanitized before it reaches the DOM, so a prompt-injected page cannot run script in the SPA origin." },

    { id: "rest", group: "transport", col: 1, row: 0, label: "REST", sub: "/api JSON", desc: "Request/response endpoints for chat, sessions, workspace, git, config, and more." },
    { id: "sse", group: "transport", col: 1, row: 1, label: "SSE", sub: "tokens + events", desc: "Server-Sent Events carry streamed chat tokens and per-session agent/cron events." },
    { id: "ws", group: "transport", col: 1, row: 2, label: "WebSocket", sub: "audio · PTY · LSP", desc: "Binary PCM16 audio in, plus the terminal PTY and the LSP proxy channel." },

    { id: "chatR", group: "server", col: 2, row: 0, label: "chat", sub: "chat/routes.py", desc: "The streaming state machine: builds the prompt, invokes the model, and drives the agentic tool loop." },
    { id: "sessR", group: "server", col: 3, row: 0, label: "sessions", sub: "sessions.py", desc: "List, load, rename, delete, and persist sessions; the per-session UPSERT with merge." },
    { id: "wsR", group: "server", col: 2, row: 1, label: "workspace", sub: "workspace.py", desc: "Connect a folder and serve the file tree, reads, and validated writes." },
    { id: "gitR", group: "server", col: 3, row: 1, label: "git", sub: "git/router.py", desc: "Status, diff, log, blame, branches, and PR helpers over the connected repo." },
    { id: "apprR", group: "server", col: 2, row: 2, label: "approval", sub: "approval/router.py", desc: "The consent endpoint: issues a nonce and executes only the exact approved action." },
    { id: "cronR", group: "server", col: 3, row: 2, label: "cron + tasks", sub: "schedulers", desc: "Cron scheduling and the background task tracker. config, plugins, and the LSP proxy mount here too." },

    { id: "trouter", group: "tools", col: 4, row: 0, label: "tool_router", sub: "dispatch", desc: "Pure dispatch: maps each tool call to its executor." },
    { id: "texec", group: "tools", col: 4, row: 1, label: "tool_executor", sub: "SSE + result shaping", desc: "Runs the batch (read-safe tools in parallel, writes serialized), emits side-effect events, and shapes results." },
    { id: "agents", group: "agents", col: 4, row: 2, label: "agent runtime", sub: "spawn · teams", desc: "spawn_agent and team_create run parallel sub-agents, each with its own tool loop." },
    { id: "ebus", group: "agents", col: 4, row: 3, label: "event bus", sub: "per-session pub/sub", desc: "Fans sub-agent and team activity out to the browser over SSE." },

    { id: "ex_web", group: "tools", col: 5, row: 0, label: "web", sub: "search · fetch", desc: "Web search and fetch executors; reach Tavily when a key is configured." },
    { id: "ex_code", group: "tools", col: 5, row: 1, label: "code", sub: "run_python", desc: "Runs Python for calculation and parsing, validated and sandboxed." },
    { id: "ex_term", group: "tools", col: 5, row: 2, label: "terminal", sub: "PTY run", desc: "The hidden terminal_run path: a real PTY, validated then sandboxed." },
    { id: "ex_git", group: "tools", col: 5, row: 3, label: "git", sub: "status · diff · commit", desc: "Git operations against the workspace repo." },
    { id: "ex_mem", group: "tools", col: 5, row: 4, label: "memory", sub: "recall · write", desc: "Reads and writes the cross-session memory store. content, search, and other executors sit alongside these." },

    { id: "validator", group: "security", col: 6, row: 0, label: "command validator", sub: "deny-list", desc: "Regex + AST deny-list in front of the sandbox: catches rm -rf /, mkfs, sensitive reads." },
    { id: "apprReg", group: "security", col: 6, row: 1, label: "approval registry", sub: "server nonce", desc: "Holds the single-use nonce for a risky command; the client cannot fabricate one." },
    { id: "sandbox", group: "security", col: 6, row: 2, label: "sandbox", sub: "sandbox-exec / bwrap", desc: "The OS jail around every shell and code path, with a secret-store deny-list. The boundary trusted last and most." },

    { id: "whisper", group: "local", col: 6, row: 3, label: "Whisper / Parakeet", sub: "ASR", desc: "On-device transcription. Audio bytes never leave the machine." },
    { id: "diarize", group: "local", col: 6, row: 4, label: "Resemblyzer", sub: "diarization", desc: "Speaker embeddings and clustering, applied on the orchestrator side." },
    { id: "index", group: "index", col: 6, row: 5, label: "index + search", sub: "embeddings · GraphRAG", desc: "Workspace embeddings, reranking, entities, and retrieval grounding." },
    { id: "sched", group: "local", col: 6, row: 6, label: "APScheduler", sub: "cron jobs", desc: "Fires scheduled jobs, which run a chat turn in the background." },

    { id: "db", group: "persist", kind: "store", col: 7, row: 0, label: "SQLite (WAL)", sub: "sessions · costs · cron", desc: "The single database for sessions, the cost log, and cron runs." },
    { id: "wsfs", group: "persist", kind: "store", col: 7, row: 1, label: "Workspace files", sub: "+ WHISPER.md", desc: "Your connected project files and the project WHISPER.md." },
    { id: "cfg", group: "persist", kind: "store", col: 7, row: 2, label: "config.json", sub: "+ env overlay", desc: "Per-machine config at the repo root, with the TAVILY_API_KEY env overlay on top." },
    { id: "memdir", group: "persist", kind: "store", col: 7, row: 3, label: "data/memory", sub: "two-tier", desc: "The two-tier memory store (global + project), injected into later prompts." },

    { id: "bedrock", group: "external", kind: "external", col: 7, row: 4, label: "Amazon Bedrock", sub: "Claude · GPT-5", desc: "The only cloud LLM call, made only when you submit a chat message." },
    { id: "mcp", group: "external", kind: "external", col: 7, row: 5, label: "MCP servers", sub: "stdio, optional", desc: "External tool providers spawned as child processes over stdio. They never see HTTP." },
    { id: "tavily", group: "external", kind: "external", col: 7, row: 6, label: "Tavily", sub: "web search", desc: "Optional web-search provider for the research tools." }
  ],
  edges: [
    { from: "spa", to: "rest" },
    { from: "spa", to: "ws", label: "audio" },
    { from: "spa", to: "sanitize", label: "render" },
    { from: "rest", to: "chatR" },
    { from: "rest", to: "sessR" },
    { from: "rest", to: "wsR" },
    { from: "rest", to: "gitR" },
    { from: "chatR", to: "sse", label: "stream" },
    { from: "sse", to: "stores" },
    { from: "chatR", to: "bedrock", label: "invoke" },
    { from: "chatR", to: "trouter" },
    { from: "chatR", to: "agents" },
    { from: "chatR", to: "db", label: "persist" },
    { from: "chatR", to: "index", label: "grounding" },
    { from: "trouter", to: "texec" },
    { from: "texec", to: "ex_web" },
    { from: "texec", to: "ex_code" },
    { from: "texec", to: "ex_term" },
    { from: "texec", to: "ex_git" },
    { from: "texec", to: "ex_mem" },
    { from: "texec", to: "mcp" },
    { from: "agents", to: "ebus" },
    { from: "ebus", to: "sse", label: "events" },
    { from: "apprR", to: "apprReg" },
    { from: "apprReg", to: "sandbox", label: "nonce" },
    { from: "ex_code", to: "validator" },
    { from: "ex_term", to: "validator" },
    { from: "validator", to: "sandbox" },
    { from: "ws", to: "whisper", label: "audio" },
    { from: "whisper", to: "diarize" },
    { from: "whisper", to: "db" },
    { from: "wsR", to: "wsfs" },
    { from: "gitR", to: "wsfs" },
    { from: "cronR", to: "sched" },
    { from: "sched", to: "chatR", label: "fire" },
    { from: "ex_web", to: "tavily" },
    { from: "ex_mem", to: "memdir" }
  ]
});
