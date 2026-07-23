/* Tutorial - scheduled tasks (cron): ask once, results land back in chat */
WSDiagram.mount("cron-flow-diagram", {
  title: "A cron job from request to inline result",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "In the browser" },
    server: { label: "Scheduler" },
    agents: { label: "Execution" }
  },
  nodes: [
    { id: "ask", group: "browser", col: 0, row: 0, label: "Ask for a schedule", sub: "every 10 min...", desc: "Describe the recurring task in plain language, e.g. 'every 10 minutes, check our SageMaker endpoints'." },
    { id: "create", group: "server", col: 1, row: 0, label: "cron_create", sub: "job saved", desc: "Claude calls the cron_create tool. The job is written to disk and a 'Cron scheduled' pill appears inline." },
    { id: "sched", group: "server", col: 2, row: 0, label: "Scheduler", sub: "APScheduler", desc: "APScheduler arms the job on its schedule (interval, wall-clock, or one-shot) inside the running server." },
    { id: "fire", group: "server", col: 3, row: 0, label: "Fires on time", sub: "on schedule", desc: "When the trigger comes due, the scheduler fires the job. Overlapping runs are coalesced." },
    { id: "agent", group: "agents", col: 4, row: 0, label: "Runs the prompt", sub: "Bedrock tool loop", desc: "The saved prompt runs through Bedrock with a small tool loop and produces a concise status report." },
    { id: "card", group: "browser", col: 5, row: 0, label: "Result card in chat", sub: "even on other session", desc: "The result lands as a card in the originating conversation, even if you are viewing a different session." }
  ],
  edges: [
    { from: "ask", to: "create" },
    { from: "create", to: "sched" },
    { from: "sched", to: "fire" },
    { from: "fire", to: "agent" },
    { from: "agent", to: "card" },
    { from: "sched", to: "fire", label: "repeat" }
  ]
});
