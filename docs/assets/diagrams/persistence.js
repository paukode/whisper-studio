/* Architecture - persistence: startup migrations, the SQLite database, and the session latch */
WSDiagram.mount("persistence-diagram", {
  title: "Persistence: startup, schema, and the session latch",
  grid: { nodeW: 172, nodeH: 62, gapX: 56, gapY: 40 },
  groups: {
    server: { label: "Server" }, persist: { label: "SQLite" }
  },
  nodes: [
    { id: "app", group: "server", col: 0, row: 0, label: "App startup", sub: "lifespan", desc: "The FastAPI lifespan runs once on boot, before any request is served." },
    { id: "mig", group: "server", col: 1, row: 0, label: "Run migrations", sub: "migrations/runner.py", desc: "Numbered migration modules are discovered and applied in order; the schema_version table tracks what has already run." },
    { id: "db", group: "persist", kind: "store", col: 2, row: 0, label: "SQLite (WAL)", sub: "storage/sessions.db", desc: "One database file in WAL mode: busy_timeout=5000, synchronous=NORMAL. Readers and a single writer proceed concurrently." },
    { id: "sess", group: "persist", kind: "store", col: 3, row: 0, label: "sessions", sub: "chat_history · segments", desc: "The main table: id, title flags, timestamps, JSON transcript segments, JSON chat_history, speaker_names, workspace_path, pinned, archived, goal, goal_state." },
    { id: "cost", group: "persist", kind: "store", col: 3, row: 1, label: "session_costs", sub: "per-call token counts", desc: "One row per billed model call: token counts, cache reads and writes, source and count provenance, and API duration; priced at read time. Backs the Costs tab and the spend caps." },
    { id: "cron", group: "persist", kind: "store", col: 3, row: 2, label: "cron_runs", sub: "run history", desc: "Per-job scheduled-task run history with a status lease (running / ok / failed) so a fire interrupted by a restart can be reconciled." },
    { id: "latch", group: "server", col: 4, row: 0, label: "Session latch", sub: "freeze config", desc: "On a session's first turn, model / effort / chat_models are snapshotted in memory and reused for the rest of the session. A restart drops the snapshot." }
  ],
  edges: [
    { from: "app", to: "mig", label: "boot" },
    { from: "mig", to: "db", label: "apply" },
    { from: "db", to: "sess" },
    { from: "db", to: "cost" },
    { from: "db", to: "cron" },
    { from: "sess", to: "latch", label: "per session" }
  ]
});
