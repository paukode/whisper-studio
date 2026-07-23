/* Home page - compact system overview (interactive demo) */
WSDiagram.mount("home-diagram", {
  title: "Whisper Studio at a glance",
  w: 900, h: 400,
  groups: {
    browser: { label: "Browser" }, transport: { label: "Transport" },
    server: { label: "Server" }, tools: { label: "Tools" },
    local: { label: "On-device" }, external: { label: "Cloud" }, persist: { label: "Storage" }
  },
  zones: [
    { label: "Browser tab", group: "browser", x: 24, y: 140, w: 188, h: 122 },
    { label: "Same origin", group: "transport", x: 232, y: 140, w: 188, h: 122 },
    { label: "FastAPI · local process", group: "server", x: 452, y: 74, w: 200, h: 256 },
    { label: "Models & storage", group: "persist", x: 682, y: 46, w: 204, h: 300 }
  ],
  nodes: [
    { id: "spa", group: "browser", x: 40, y: 168, w: 158, h: 64, label: "React SPA", sub: "chat · transcript · IDE", desc: "The single-page app: chat, live transcript, and the full workspace IDE." },
    { id: "wire", group: "transport", x: 248, y: 172, w: 158, h: 56, label: "HTTP · SSE · WS", sub: "one port", desc: "REST + Server-Sent Events + WebSocket, all on http://127.0.0.1:8000." },
    { id: "api", group: "server", x: 468, y: 102, w: 168, h: 58, label: "FastAPI server", sub: "server/main.py", desc: "Serves the SPA and the API from one process; dispatches every request." },
    { id: "exec", group: "tools", x: 468, y: 248, w: 168, h: 58, label: "Tool executors", sub: "executors/", desc: "Runs the assistant's tool calls: files, git, web, code, memory." },
    { id: "asr", group: "local", x: 698, y: 68, w: 172, h: 60, label: "Whisper / Parakeet", sub: "on-device ASR", desc: "Transcription runs locally. Audio never leaves the machine." },
    { id: "bedrock", group: "external", kind: "external", x: 698, y: 170, w: 172, h: 60, label: "Amazon Bedrock", sub: "Claude models", desc: "The only cloud LLM call. Sent only when you submit a chat message." },
    { id: "store", group: "persist", kind: "store", x: 698, y: 272, w: 172, h: 60, label: "Workspace + SQLite", sub: "files · sessions", desc: "Your files plus the WAL-mode SQLite session database." }
  ],
  edges: [
    { from: "spa", to: "wire" },
    { from: "wire", to: "api" },
    { from: "api", to: "exec" },
    { from: "api", to: "asr", label: "audio" },
    { from: "api", to: "bedrock", label: "chat" },
    { from: "exec", to: "store" }
  ]
});
