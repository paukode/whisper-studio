/* Tutorial - choosing how to ask: a one-shot prompt, a goal that runs until
   done, or a workflow that fans out. */
WSDiagram.mount("goals-tut-diagram", {
  title: "Which one do I reach for?",
  grid: { nodeW: 182, nodeH: 62, gapX: 62, gapY: 44 },
  groups: {
    server: { label: "Your task" }, model: { label: "How to ask" },
    security: { label: "Completion gate" }, persist: { label: "Done" }
  },
  nodes: [
    { id: "task", group: "server", col: 0, row: 1, label: "A task to do", sub: "a question, or work", desc: "Start here. Usually, you type a message. Use a goal or a workflow only for a task with many steps or with parts that run in parallel." },
    { id: "prompt", group: "model", col: 1, row: 0, label: "Just prompt", sub: "a question · a small edit", desc: "Type the prompt and press Enter. One turn, or a short tool loop. This is the default, and the correct method for a quick or small task." },
    { id: "goal", group: "model", col: 1, row: 1, label: "/goal <text>", sub: "run until done", desc: "A task with many steps that must continue until it is complete. The command sets the goal and sends the same text as the first message, so the work starts immediately. The gate then examines each turn against the goal." },
    { id: "workflow", group: "model", col: 1, row: 2, label: "a workflow", sub: "wide · parallel", desc: "Wide work, work in parallel, or work that a second agent examines: an audit of 20 files, or five designs and a judge. The model writes a script that starts subagents. You approve the script first." },
    { id: "gate", group: "security", col: 2, row: 1, label: "completion gate", sub: "achieved?", desc: "At the end of each turn, the gate runs the Stop hooks. It examines the files that the reply says it wrote, and a passed check after the last code change. It runs your /goal gate add commands. Then a cheap evaluator judges the goal (a VERIFY PASS or FAIL from verify_change has more weight than text). If the goal is not met, the gate tells the model what is missing, and the turn continues." },
    { id: "done", group: "persist", col: 3, row: 1, kind: "store", label: "done", sub: "goal achieved (capped)", desc: "When the evaluator says that the goal is achieved, the turn ends and the goal is cleared. A limit of 8 blocks in sequence (the default) stops a task that does not progress." }
  ],
  edges: [
    { from: "task", to: "prompt", label: "quick" },
    { from: "task", to: "goal", label: "multi-step" },
    { from: "task", to: "workflow", label: "parallel" },
    { from: "goal", to: "gate" },
    { from: "gate", to: "goal", label: "not yet" },
    { from: "gate", to: "done", label: "achieved" }
  ]
});
