/* Tutorial - scheduled tasks (cron): ask once, results land back in chat */
WSDiagram.mount("cron-flow-diagram", {
  title: "A cron job from request to inline result",
  grid: { nodeW: 168, nodeH: 60, gapX: 54, gapY: 40 },
  groups: {
    browser: { label: "In the app" },
    server: { label: "Scheduler" },
    agents: { label: "Execution" }
  },
  nodes: [
    { id: "ask", group: "browser", col: 0, row: 0, label: "Ask for a schedule", sub: "every 10 min...", desc: "Describe the task and its schedule in usual words, for example 'every 10 minutes, check our SageMaker endpoints'." },
    { id: "create", group: "server", col: 1, row: 0, label: "cron_create", sub: "job saved", desc: "The assistant calls the cron_create tool. The app writes the job to disk, and a 'Cron ... scheduled' pill shows in the chat." },
    { id: "sched", group: "server", col: 2, row: 0, label: "Scheduler", sub: "APScheduler", desc: "APScheduler starts the schedule of the job (interval, clock time, or one time) in the server." },
    { id: "fire", group: "server", col: 3, row: 0, label: "Fires on time", sub: "on schedule", desc: "At the run time, the scheduler runs the job. If runs overlap, the scheduler joins them into one run." },
    { id: "agent", group: "agents", col: 4, row: 0, label: "Runs the prompt", sub: "same engine as chat", desc: "The saved prompt runs on the same turn engine and tool pool as the chat. It uses the Anthropic model of the job, or else haiku, sonnet5.5, or the first configured chat model. It does not use the model of the chat, and it refuses on-device models. In Local mode, the app refuses the run and keeps the job." },
    { id: "card", group: "browser", col: 5, row: 0, label: "Result card in chat", sub: "even on other session", desc: "The result shows as a card in the conversation that made the job, also when you look at a different session." }
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
