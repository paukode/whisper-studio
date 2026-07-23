/* Architecture - the blocking-hooks engine: PreToolUse / PostToolUse / Stop
   gates evaluated in-process then as sandboxed shell hooks. */
WSDiagram.mount("hooks-diagram", {
  title: "How a hook gates a tool call",
  grid: { nodeW: 176, nodeH: 60, gapX: 56, gapY: 46 },
  groups: {
    server: { label: "Tool pipeline" }, security: { label: "Hooks engine" },
    external: { label: "Shell hooks" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "executor", group: "server", col: 0, row: 1, label: "Tool executor", sub: "execute_tool_batch", desc: "Before running a tool, the executor calls the hooks engine for PreToolUse; after, for PostToolUse. Subagents and cron runs call the same engine." },
    { id: "pre", group: "security", col: 1, row: 0, label: "PreToolUse gate", sub: "deny · rewrite", desc: "run_hooks('PreToolUse', payload). A hook may DENY the call (a security_blocked / hook_blocked frame with the reason) or REWRITE its input via updatedInput before it runs." },
    { id: "engine", group: "security", col: 2, row: 1, label: "hooks engine", sub: "run_hooks(event, payload)", desc: "server/hooks/engine.py evaluates every hook for an event in two phases: first in-process (plugins + the orchestrator gate), then shell hooks — user hooks then trusted project hooks — filtered by the hook's matcher." },
    { id: "plugins", group: "security", col: 3, row: 0, label: "in-process phase", sub: "plugins + gate", desc: "Python plugin hooks and the orchestrator's own gate run first; a control dict can short-circuit the whole event before any shell runs." },
    { id: "shell", group: "external", col: 3, row: 1, kind: "external", label: "shell phase", sub: "sandboxed · JSON stdin", desc: "A matched shell hook runs sandboxed with the event payload on stdin (run_sandboxed input_data). Exit 0 = pass, exit 2 = block (stderr is the reason), other/timeout honors the hook's on_error (ignore|block). Structured stdout {decision, reason, updatedInput, additionalContext} is parsed." },
    { id: "run", group: "server", col: 1, row: 2, label: "tool runs", sub: "if allowed", desc: "Only if no PreToolUse hook denied does the tool execute — with the possibly-rewritten input." },
    { id: "post", group: "security", col: 2, row: 2, label: "PostToolUse", sub: "additionalContext", desc: "run_hooks('PostToolUse', payload) after the tool. A hook's additionalContext is folded into the tool result the model sees next." },
    { id: "stop", group: "security", col: 0, row: 0, label: "Stop gate", sub: "check_stop_hooks", desc: "At end of turn, check_stop_hooks() can refuse to end the turn (a stop_hook_feedback / stop_hook_block frame), bounded by MAX_STOP_BLOCKS_PER_TURN so it can't loop forever. This is where the WS-E goal gate registers." },
    { id: "panel", group: "browser", col: 3, row: 2, label: "HooksPanel", sub: "CRUD · Test · trust", desc: "The Settings panel manages hooks (/api/hooks CRUD + /test dry-run) and shows a project-trust banner: project hooks stay inert until SHA-256-approved." }
  ],
  edges: [
    { from: "executor", to: "pre" },
    { from: "pre", to: "engine" },
    { from: "engine", to: "plugins" },
    { from: "engine", to: "shell" },
    { from: "pre", to: "run", label: "allow" },
    { from: "run", to: "post" },
    { from: "post", to: "engine" },
    { from: "stop", to: "engine", label: "Stop" },
    { from: "panel", to: "engine", label: "manages" }
  ]
});
