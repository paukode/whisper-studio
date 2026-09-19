/* Architecture - the goal loop: an end-of-turn completion gate that runs Stop
   hooks, deterministic checks and a cheap goal evaluator, re-injecting
   feedback until achieved. */
WSDiagram.mount("goals-diagram", {
  title: "The completion gate at the end of a turn",
  grid: { nodeW: 178, nodeH: 60, gapX: 54, gapY: 46 },
  groups: {
    server: { label: "Turn loop" }, security: { label: "Deterministic checks" },
    agents: { label: "Evaluator" }, persist: { label: "Store" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "turn", group: "server", col: 0, row: 1, label: "End of turn", sub: "model returned end_turn", desc: "When the model stops without a tool call, the engine does NOT end the turn immediately: it runs the completion gate first. This is the interactive cloud-chat policy only; sub-agents, cron, headless turns, and local chat all run with the gate off." },
    { id: "gate", group: "server", col: 1, row: 1, label: "run_completion_gate", sub: "server/goals/gate.py", desc: "Ordered cheapest and most deterministic first, first block wins: Stop hooks, claimed deliverables, verification evidence, the goal's quality gates, then the evaluator. The first three run with or without a goal; the last two need the goal_loop flag on and an active goal. Every phase fails OPEN: an error never traps the turn." },
    { id: "stop", group: "security", col: 2, row: 0, label: "Hooks, files, evidence", sub: "before any model call", desc: "Stop hooks first, then: do the files the reply claims exist and is an artifact claim backed by a call this turn, and did a test, lint, typecheck or build run green after the last code edit. All three run even with the goal flag off. Any block re-injects its reason and the turn continues." },
    { id: "gates", group: "security", col: 2, row: 2, label: "Quality gates", sub: "/goal gate add", desc: "Shell commands stored on the goal that must exit 0. They run before the evaluator: a red gate is proof the goal is not met, so its output tail becomes the feedback and the evaluator is skipped. Three failures in a row pause the goal." },
    { id: "eval", group: "agents", col: 2, row: 1, label: "Goal evaluator", sub: "cheap model verdict", desc: "server/goals/evaluator.py sends the goal + a rendered transcript tail to a cheap auxiliary model (auxiliary_models.goal_evaluator, Haiku by default) and gets {achieved | not_achieved | blocked, feedback, confidence}. A confident 'blocked' (>=0.7) ends the turn rather than looping." },
    { id: "tail", group: "server", col: 3, row: 1, label: "transcript tail", sub: "tail.py, head+tail 12KB", desc: "A provider-neutral renderer flattens the recent turn (Anthropic blocks OR Responses items) into text for the evaluator, head+tail-sliced to a 12KB cap." },
    { id: "verify", group: "agents", col: 3, row: 2, label: "verify_change tool", sub: "VERIFY PASS / FAIL", desc: "server/prompt_tools/verify_change.py runs the repo's gate and emits a deterministic final line the evaluator weighs above prose, the same signal a CI autofix workflow ends on." },
    { id: "store", group: "persist", col: 1, row: 2, kind: "store", label: "goal + goal_state", sub: "sessions row (migration 009)", desc: "server/goals/store.py keeps the goal, its state (including the consecutive-block counter) and its quality gates on the sessions row; the counter resets on each new user turn." },
    { id: "loop", group: "server", col: 0, row: 0, label: "re-inject + continue", sub: "capped at 8 blocks", desc: "Any block folds its feedback into the turn and loops. One shared consecutive-block cap (goal_max_consecutive_blocks, default 8) backstops every check; at the cap a goal_cap_reached frame names which one was asking and the turn ends." },
    { id: "banner", group: "browser", col: 0, row: 2, label: "GoalBanner · /goal", sub: "set / clear the goal", desc: "/goal <text> sets the session goal AND sends the text as the first message; /goal clear ends it; /goal gate add <command> adds a must-pass check. GoalBanner sits above the composer. Frames goal_eval / goal_cap_reached / stop_hook_block render the gate's decisions." }
  ],
  edges: [
    { from: "turn", to: "gate" },
    { from: "gate", to: "stop" },
    { from: "gate", to: "gates" },
    { from: "gates", to: "eval", label: "all green" },
    { from: "eval", to: "tail" },
    { from: "verify", to: "eval" },
    { from: "eval", to: "store", label: "record" },
    { from: "eval", to: "loop", label: "not achieved" },
    { from: "stop", to: "loop", label: "block" },
    { from: "gates", to: "loop", label: "red gate" },
    { from: "banner", to: "store", label: "set goal" }
  ]
});
