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
    { id: "task", group: "server", col: 0, row: 1, label: "A task to do", sub: "a question, or work", desc: "Start here. Most of the time you just type — the extra machinery is only worth it when a task has many steps or wants real parallelism." },
    { id: "prompt", group: "model", col: 1, row: 0, label: "Just prompt", sub: "a question · a small edit", desc: "Type it and press Enter. One turn, or a short tool loop. This is the default and the right choice for anything quick or well-scoped." },
    { id: "goal", group: "model", col: 1, row: 1, label: "/goal <text>", sub: "run until done", desc: "A multi-step task where you want Whisper to keep going until it is genuinely finished, not stop halfway. Set a goal and each turn is checked against it." },
    { id: "workflow", group: "model", col: 1, row: 2, label: "a workflow", sub: "wide · parallel", desc: "Wide, parallel, or adversarially-verified work — audit twenty files, generate five designs and judge them. The model writes a script that fans out subagents; you approve it first." },
    { id: "gate", group: "security", col: 2, row: 1, label: "completion gate", sub: "achieved?", desc: "At the end of each turn the gate runs Stop hooks and a cheap evaluator (and weighs a VERIFY PASS/FAIL from the verify-change skill). If the goal is not met, it feeds back what's missing and the turn continues." },
    { id: "done", group: "persist", col: 3, row: 1, kind: "store", label: "done", sub: "goal achieved (capped)", desc: "When the evaluator says the goal is achieved, the turn ends. A consecutive-block cap (default 8) backstops a stuck task so it can never loop forever." }
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
