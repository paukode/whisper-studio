/* Architecture - cost tracking, budgets, and forecasting */
WSDiagram.mount("costs-diagram", {
  title: "Cost logging, aggregation, budgets, and forecast",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 42 },
  groups: {
    server: { label: "Cost pipeline" }, persist: { label: "Storage" },
    security: { label: "Budget" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "turn", group: "server", col: 0, row: 0, label: "Turn completes", sub: "message_delta", desc: "Each streaming round ends with a message_delta carrying the token usage for that round." },
    { id: "est", group: "server", col: 1, row: 0, label: "estimate_cost", sub: "in / out / cache tokens", desc: "A USD estimate from the per-model price table: input, output, cache-read, and cache-write rates per 1M tokens." },
    { id: "log", group: "persist", kind: "store", col: 2, row: 0, label: "session_costs table", sub: "one row per turn", desc: "record_turn INSERTs one row per turn into session_costs in sessions.db: model, tokens, cost_usd, api_duration_ms, created_at." },
    { id: "sess", group: "server", col: 3, row: 0, label: "Session totals", sub: "aggregate", desc: "get_session_summary sums a session's rows: turns, tokens, cache reads, total cost, and duration." },
    { id: "check", group: "security", col: 4, row: 0, label: "check_budget", sub: "session / day cap", desc: "Before every round, compares the running session and daily totals against the configured caps and stops the turn if a cap is hit." },
    { id: "fore", group: "server", col: 3, row: 1, label: "Forecast", sub: "turns remaining", desc: "estimate_remaining_turns projects context usage against the compaction threshold from the average tokens per turn." },
    { id: "ui", group: "browser", col: 4, row: 1, label: "Costs panel", sub: "usage + estimate", desc: "The SPA Costs panel reads the aggregates and renders per-model totals, the daily bar chart, and the live per-turn estimate." }
  ],
  edges: [
    { from: "turn", to: "est", label: "usage" },
    { from: "est", to: "log", label: "record_turn" },
    { from: "log", to: "sess" },
    { from: "sess", to: "check", label: "before round" },
    { from: "log", to: "fore" },
    { from: "sess", to: "ui" },
    { from: "fore", to: "ui" }
  ]
});
