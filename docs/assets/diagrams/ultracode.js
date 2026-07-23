/* Architecture - the ultracode workflow runtime: script -> approval -> detached
   Node-vm execution proxying agent() over JSON-RPC into Python. */
WSDiagram.mount("ultracode-diagram", {
  title: "How a workflow runs: author, approve, execute, resume",
  grid: { nodeW: 176, nodeH: 62, gapX: 54, gapY: 44 },
  groups: {
    model: { label: "Model" }, server: { label: "Python runtime" },
    security: { label: "Node vm harness" }, transport: { label: "JSON-RPC" },
    agents: { label: "Agents" }, persist: { label: "Journal" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "author", group: "model", col: 0, row: 0, label: "Model authors script", sub: "export const meta {…}", desc: "In the ultracode tier the model writes a deterministic JS orchestration script: a pure meta literal (name, description, phases) plus a body that calls agent() / pipeline() / parallel() / phase() / log()." },
    { id: "tool", group: "server", col: 1, row: 0, label: "workflow_run", sub: "parse + preview", desc: "server/workflows/tools.py spawns Node once to extract and validate the meta literal, then returns an approval preview side-effect. A brand-new inline script never auto-runs; a trusted saved workflow or a resume launches immediately." },
    { id: "preview", group: "browser", col: 2, row: 0, label: "Approval preview", sub: "phases + full script", desc: "WorkflowPreviewCard shows the phases as a stepper and the whole script. Upfront human approval of every new script is the PRIMARY trust anchor for the vm. Approving POSTs to the launch route." },
    { id: "manager", group: "server", col: 1, row: 1, label: "manager.start_run", sub: "detached + workflow_runs row", desc: "server/workflows/manager.py writes a workflow_runs row (migration 009), snapshots the script immutably, and drives the run detached on the server loop. Boot reconcile flips orphaned rows to 'stale' (journal stays resumable)." },
    { id: "runtime", group: "server", col: 2, row: 1, label: "WorkflowRun", sub: "sem 16 · cap 1000 · $budget", desc: "server/workflows/runtime.py owns the limits the script cannot bypass: a 16-way concurrency semaphore, a 1000-agent lifetime cap, a USD budget re-checked on each slot, and the resume cache. It is the JSON-RPC server for the harness." },
    { id: "harness", group: "security", col: 3, row: 1, kind: "store", label: "Node-24 vm harness", sub: "guards + api.mjs", desc: "harness.mjs runs the script in a locked-down vm context: absent globals (no fs / process / require / fetch), guards.mjs makes Math.random / Date.now / new Date() / timers throw, and node runs with --disallow-code-generation-from-strings so Function('…') can't reach the host." },
    { id: "rpc", group: "transport", col: 3, row: 2, label: "ndjson JSON-RPC", sub: "over stdio", desc: "server/workflows/rpc.py frames line-delimited JSON-RPC on stdio. agent() and nested workflow() calls in the script are proxied to Python; every other statement is pure in-sandbox JS." },
    { id: "adapter", group: "server", col: 2, row: 2, label: "agent_adapter", sub: "one agent() → run_agent", desc: "server/workflows/agent_adapter.py maps each agent() RPC to one WS-C run_agent call (schema / effort / model / worktree opts) and normalizes the AgentResult back to {text, output, usage, status}." },
    { id: "agents", group: "agents", col: 1, row: 2, label: "run_agent", sub: "Claude / GPT subagent", desc: "The provider-agnostic runtime runs a real tool-using subagent on Anthropic or OpenAI-on-Bedrock. Its token usage rolls into the run's ledger; structured_schema returns a validated object." },
    { id: "journal", group: "persist", col: 2, row: 3, kind: "store", label: "journal + resume cache", sub: "append-only, by seq", desc: "server/workflows/journal.py records each phase and agent call by issue-order seq. A resume replays cached results for the longest unchanged prefix and only re-runs from the first edited call onward." },
    { id: "sse", group: "transport", col: 3, row: 3, label: "per-run SSE", sub: "channel workflow:{id}", desc: "The run publishes phase / agent / completion events on a per-run event-bus channel, so a reloaded session re-attaches and sees live progress." },
    { id: "card", group: "browser", col: 4, row: 2, label: "WorkflowRunCard", sub: "live phases · cost · stop", desc: "The SPA renders live phase, agent count, and spend with Stop / Resume, folding the terminal completion into the run row (workflowStore, keyed by run_id)." }
  ],
  edges: [
    { from: "author", to: "tool" },
    { from: "tool", to: "preview", label: "preview" },
    { from: "preview", to: "manager", label: "approve" },
    { from: "manager", to: "runtime" },
    { from: "runtime", to: "harness", label: "spawn vm" },
    { from: "harness", to: "rpc", label: "agent()" },
    { from: "rpc", to: "adapter" },
    { from: "adapter", to: "agents" },
    { from: "agents", to: "adapter", label: "result" },
    { from: "runtime", to: "journal" },
    { from: "journal", to: "sse" },
    { from: "sse", to: "card" }
  ]
});
