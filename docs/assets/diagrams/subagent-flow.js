/* Tutorial - how a team fans out and reports back over the event bus */
WSDiagram.mount("subagent-flow-diagram", {
  title: "Spawn a team, watch it report back",
  grid: { nodeW: 168, nodeH: 60, gapX: 56, gapY: 44 },
  groups: {
    browser: { label: "Chat" }, agents: { label: "Agents" },
    server: { label: "Runtime" }
  },
  nodes: [
    { id: "task", group: "browser", col: 0, row: 0.5, label: "Big task", sub: "one prompt", desc: "You describe work that has independent parts. Or you type /subagent for one focused helper, which runs in the background in its own message." },
    { id: "spawn", group: "agents", col: 1, row: 0.5, label: "spawn_agent / team_create", sub: "from chat", desc: "The assistant can run the parts at the same time. team_create starts a team of agents. spawn_agent starts one independent agent. Maximum 16 agents run at the same time in one session." },
    { id: "a1", group: "agents", col: 2, row: 0, label: "Agent A", sub: "explore", desc: "A read-only agent that searches and reads to get the facts, and then returns short findings." },
    { id: "a2", group: "agents", col: 2, row: 1, label: "Agent B", sub: "verify", desc: "Runs its own tool loop, examines the work, and ends with VERDICT: PASS, FAIL, or PARTIAL." },
    { id: "bus", group: "server", kind: "store", col: 3, row: 0.5, label: "Event bus", sub: "team_progress", desc: "Each agent sends the started, turn_start, tool_call, tool_result, and completed events to a channel for each session." },
    { id: "report", group: "browser", col: 4, row: 0.5, label: "Agent rows", sub: "live + final", desc: "The chat shows one line for the team and one row for each agent. The rows change during the run. Open on a row shows the full feed and the report of that agent in the agent pane of the dock." }
  ],
  edges: [
    { from: "task", to: "spawn" },
    { from: "spawn", to: "a1", label: "fan out" },
    { from: "spawn", to: "a2" },
    { from: "a1", to: "bus", label: "publish" },
    { from: "a2", to: "bus" },
    { from: "bus", to: "report", label: "team_progress" }
  ]
});
