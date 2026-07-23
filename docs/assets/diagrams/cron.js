/* Architecture - cron / scheduled tasks: from create to inline chat delivery */
WSDiagram.mount("cron-diagram", {
  title: "Cron lifecycle: create, schedule, fire, deliver",
  grid: { nodeW: 162, nodeH: 60, gapX: 46, gapY: 44 },
  groups: {
    server: { label: "Runtime" }, persist: { label: "Storage" },
    agents: { label: "Agent + bus" }, browser: { label: "Chat" }
  },
  nodes: [
    { id: "create", group: "server", col: 0, row: 0, label: "Create job", sub: "/api/cron/create", desc: "A tool call or the cron panel validates the schedule and mints a job (interval, cron, or at)." },
    { id: "store", group: "persist", kind: "store", col: 1, row: 0, label: "cron_jobs.json", sub: "locked write", desc: "Jobs persist to data/cron_jobs.json via an atomic temp-file write under a file lock." },
    { id: "sched", group: "server", col: 2, row: 0, label: "APScheduler", sub: "wall-clock union", desc: "init_scheduler runs an AsyncIOScheduler; each enabled job arms an interval, cron, or date trigger." },
    { id: "lease", group: "persist", kind: "store", col: 3, row: 0, label: "Lease run", sub: "cron_runs: running", desc: "start_run opens a run lease in the cron_runs table with status 'running' and a ~15 min interrupted cutoff." },
    { id: "agent", group: "agents", col: 3, row: 1, label: "Spawn agent", sub: "job prompt", desc: "A background thread runs the job prompt through Bedrock with a short tool-use loop." },
    { id: "bus", group: "agents", col: 2, row: 1, label: "Event bus", sub: "cron_event", desc: "The finished output is published live to the originating session's subscribers." },
    { id: "sess", group: "browser", col: 1, row: 1, label: "Originating session", sub: "cron_event card", desc: "The result renders inline as a cron_event card in the chat that created the job." },
    { id: "finish", group: "persist", kind: "store", col: 3, row: 2, label: "finish_run", sub: "ok / failed", desc: "finish_run closes the lease with the outcome, next run time, and duration, then prunes old runs." }
  ],
  edges: [
    { from: "create", to: "store" },
    { from: "store", to: "sched" },
    { from: "sched", to: "lease", label: "on schedule" },
    { from: "lease", to: "agent" },
    { from: "agent", to: "bus" },
    { from: "bus", to: "sess" },
    { from: "agent", to: "finish" }
  ]
});
