/* Architecture - cost logging, read-time pricing, budgets, and forecasting */
WSDiagram.mount("costs-diagram", {
  title: "Cost logging, read-time pricing, budgets, and forecast",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 42 },
  groups: {
    server: { label: "Cost pipeline" }, persist: { label: "Storage" },
    security: { label: "Budget" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "turn", group: "server", col: 0, row: 0, label: "Billed call ends", sub: "counts from the payload", desc: "Each call's token counts come from the provider's own payload: the Anthropic usage fields and Bedrock's invocationMetrics trailer (authoritative), the Responses usage for GPT, the server's usage for local. With no count in the payload, characters / 4 of what was posted and received, marked estimated. Side tasks (titles, compaction, the classifier, ...) are logged the same way by server/costs/calls.py." },
    { id: "log", group: "persist", kind: "store", col: 1, row: 0, label: "session_costs table", sub: "one row per call", desc: "record_turn INSERTs one row per billed call: model, token counts, source, count provenance, api_duration_ms, created_at (UTC). No dollar figure is stored." },
    { id: "est", group: "server", col: 2, row: 0, label: "estimate_cost", sub: "priced when read", desc: "Every reader prices the stored counts with the current rate table (pricing.example.json overlaid by the user's own pricing.json), so a rate correction re-rates history everywhere at once." },
    { id: "check", group: "security", col: 3, row: 0, label: "check_budget", sub: "session / UTC-day cap", desc: "Before every round, compares the session's cost and the current UTC day's cost against the configured caps. At 90 percent (check_budget_soft) the turn gets one final round with tools off so it can answer with what it has; the next trip past the cap ends it." },
    { id: "fore", group: "server", col: 2, row: 1, label: "Compaction nudge", sub: "context_used / context_max", desc: "note_prompt_tokens records the real per-round prompt size; should_nudge_compaction fires once usage crosses COMPACT_NUDGE_FRACTION (0.95) of the model's usable input budget." },
    { id: "ui", group: "browser", col: 3, row: 1, label: "Costs tab", sub: "/api/costs/usage", desc: "The ranged report over UTC days: zero-filled day, week or month buckets, a split by model, session or source, cache figures for the range, and a dated note on every GPT figure." }
  ],
  edges: [
    { from: "turn", to: "log", label: "record_turn" },
    { from: "log", to: "est" },
    { from: "est", to: "check", label: "before round" },
    { from: "est", to: "ui" },
    { from: "log", to: "fore" },
    { from: "fore", to: "ui" }
  ]
});
