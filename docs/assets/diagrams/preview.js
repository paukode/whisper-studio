/* Architecture - live preview: spawn dev server, drive Playwright, watch it */
WSDiagram.mount("preview-diagram", {
  title: "Live preview and browser automation",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 44 },
  groups: {
    server: { label: "Preview manager" }, local: { label: "Headless Chromium" },
    tools: { label: "Actions" }, browser: { label: "Right dock" }
  },
  nodes: [
    { id: "start", group: "server", col: 0, row: 0.5, label: "preview_start", sub: "approval-gated", desc: "Model (or the Restart button) asks to start a named session; POST /api/preview/sessions resolves the command from .whisper/launch.json or an ad-hoc argv." },
    { id: "spawn", group: "server", col: 1, row: 0.5, label: "Spawn dev server", sub: "process.py", desc: "DevServerProcess.spawn validates the argv, wraps it in the PTY sandbox, and starts it with start_new_session (avoiding the uvloop fork deadlock)." },
    { id: "wait", group: "server", col: 2, row: 0.5, label: "Drain + keep alive", sub: "1 MB ring buffer", desc: "Two reader tasks continuously drain stdout/stderr into bounded 1 MB ring buffers so preview_logs can tail them. Spawn is bounded at 30s." },
    { id: "browser", group: "local", col: 3, row: 0.5, label: "Playwright browser", sub: "browser.py", desc: "On first navigation, an ephemeral Chromium context + page is launched lazily (60s cold-start bound). Console, page errors, and network are captured." },
    { id: "act", group: "tools", col: 4, row: 0, label: "navigate · click · fill", sub: "15s / 30s bounds", desc: "Approval-gated actions drive the page: goto (30s), click/fill/eval/resize (15s). A scheme guard blocks file://, data:, chrome://." },
    { id: "shot", group: "local", col: 4, row: 1, label: "screenshot", sub: "PNG to JPEG", desc: "preview_screenshot downscales to a JPEG (max 1280px, q70) and returns a [WS_PREVIEW_IMAGE] sentinel that tool_executor turns into a real image block the model can see." },
    { id: "dock", group: "browser", col: 5, row: 0.5, label: "Live preview dock", sub: "screencast WS", desc: "A view-only CDP screencast streams JPEG frames over /ws/preview/{name}/screencast to the SPA right dock, so you watch exactly what the model's browser does." }
  ],
  edges: [
    { from: "start", to: "spawn" },
    { from: "spawn", to: "wait" },
    { from: "wait", to: "browser", label: "on navigate" },
    { from: "browser", to: "act" },
    { from: "act", to: "shot" },
    { from: "browser", to: "dock", label: "screencast" }
  ]
});
