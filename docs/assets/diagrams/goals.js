/* Architecture - the goal loop: an end-of-turn completion gate that runs Stop
   hooks then a cheap goal evaluator and re-injects feedback until achieved. */
WSDiagram.mount("goals-diagram", {
  title: "The completion gate at the end of a turn",
  grid: { nodeW: 178, nodeH: 60, gapX: 54, gapY: 46 },
  groups: {
    server: { label: "Turn loop" }, security: { label: "Stop hooks" },
    agents: { label: "Evaluator" }, persist: { label: "Store" }, browser: { label: "SPA" }
  },
  nodes: [
    { id: "turn", group: "server", col: 0, row: 1, label: "End of turn", sub: "model returned end_turn", desc: "When the model stops without a tool call, the endpoint does NOT end the turn immediately — it runs the completion gate first. Wired in all three loops (chat, openai_bedrock, local)." },
    { id: "gate", group: "server", col: 1, row: 1, label: "run_completion_gate", sub: "server/goals/gate.py", desc: "Two phases. Phase 1 (Stop hooks) ALWAYS runs. Phase 2 (the goal evaluator) runs only when the goal_loop flag is on AND the session has an active goal. Both fail OPEN — an error never traps the turn." },
    { id: "stop", group: "security", col: 2, row: 0, label: "Phase 1: Stop hooks", sub: "check_stop_hooks", desc: "The WS-I Stop-hook gate. A block re-injects the hook's reason and the turn continues; this runs even with the goal flag off." },
    { id: "eval", group: "agents", col: 2, row: 1, label: "Phase 2: goal evaluator", sub: "Haiku verdict", desc: "server/goals/evaluator.py sends the goal + a rendered transcript tail to a cheap Haiku call and gets {achieved | not_achieved | blocked, feedback, confidence}. A confident 'blocked' (>=0.7) ends the turn rather than looping." },
    { id: "tail", group: "server", col: 3, row: 1, label: "transcript tail", sub: "tail.py, head+tail 12KB", desc: "A provider-neutral renderer flattens the recent turn (Anthropic blocks OR Responses items) into text for the evaluator, head+tail-sliced to a 12KB cap." },
    { id: "verify", group: "agents", col: 3, row: 2, label: "verify-change skill", sub: "VERIFY PASS / FAIL", desc: "skills/verify-change.md runs the repo's gate and emits a deterministic final line the evaluator weighs above prose — the same signal a CI autofix workflow ends on." },
    { id: "store", group: "persist", col: 1, row: 2, kind: "store", label: "goal + goal_state", sub: "sessions row (migration 009)", desc: "server/goals/store.py keeps the goal and a consecutive-block counter on the sessions row; the counter resets on each new user turn." },
    { id: "loop", group: "server", col: 0, row: 0, label: "re-inject + continue", sub: "capped at 8 blocks", desc: "not_achieved (or a Stop block) folds feedback into the turn and loops. A consecutive-block cap (goal_max_consecutive_blocks, default 8) backstops a stuck evaluator." },
    { id: "banner", group: "browser", col: 0, row: 2, label: "GoalBanner · /goal", sub: "set / clear the goal", desc: "The /goal command sets a session goal (and /goal clear ends it); GoalBanner sits above the composer. Frames goal_eval / goal_cap_reached render the gate's decisions." }
  ],
  edges: [
    { from: "turn", to: "gate" },
    { from: "gate", to: "stop" },
    { from: "gate", to: "eval" },
    { from: "eval", to: "tail" },
    { from: "verify", to: "eval" },
    { from: "eval", to: "store", label: "record" },
    { from: "eval", to: "loop", label: "not achieved" },
    { from: "stop", to: "loop", label: "block" },
    { from: "banner", to: "store", label: "set goal" }
  ]
});
