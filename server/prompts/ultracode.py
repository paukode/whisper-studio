"""The ultracode directive — teaches the workflow runtime (WS-D).

In ultracode mode the model authors a deterministic JS orchestration script and
runs it with ``workflow_run``. A locked-down Node harness executes it detached,
proxying every ``agent()`` back through the provider adapter with 16-way
concurrency, a 1000-agent cap, budgets, and a resumable journal.
"""

from __future__ import annotations

_BASE = """

## Ultracode mode — the workflow runtime

Ultracode is active: optimise for the most thorough, correct result; token cost
is not a constraint. For substantive, multi-part, or long-horizon work, DON'T
grind sequentially in one context — write a **workflow script** and run it with
`workflow_run`. It executes detached; you get a run_id back and poll
`workflow_status`. For trivial or conversational turns, just answer directly.

### The script

A workflow is plain JavaScript (top-level await; `return` your result). It MUST
begin with a pure `meta` literal (no function calls or variables inside it):

```js
export const meta = {
  name: "fix-and-verify",
  description: "Fix the failing tests and prove it",
  phases: ["explore", "implement", "verify"],
};

const findings = await agent("Explore how X works and where Y is defined.",
  { label: "scout", phase: "explore" });

phase("implement");
const results = await parallel(targets.map(t => () =>
  agent(`Fix ${t} given: ${findings.text}`,
        { phase: "implement", schema: FIX_SCHEMA, effort: "high" })));

phase("verify");
const verdicts = await pipeline(results.filter(Boolean),
  r => agent(`Adversarially verify this fix — try to REFUTE it: ${JSON.stringify(r.output)}`,
             { phase: "verify" }));

return { fixed: results.filter(Boolean).length, verdicts };
```

### API (all injected as globals)

- `agent(prompt, opts?) -> {text, output, usage, status, agent_id}` — spawn a
  subagent. `opts`: `label`, `phase`, `schema` (JSON Schema → `output` is the
  validated object, else null), `effort` (low|medium|high|xhigh|max),
  `isolation:"worktree"` (isolated git worktree for parallel writers),
  `agentType`, `model`. A failed agent resolves with `status:"failed"` (it does
  NOT throw) — the script decides what to do.
- `pipeline(items, ...stages)` — run each item through ALL stages independently,
  NO barrier between stages (item A can be in stage 3 while B is in stage 1).
  Each stage is `(prev, item, index) => Promise`. A stage that throws drops that
  item to null. This is the DEFAULT for multi-stage work.
- `parallel(thunks)` — a BARRIER: awaits all thunks. A thunk that throws → null,
  so `.filter(Boolean)` before using. Use only when you need ALL results at once
  (dedup/merge across the full set, early-exit on zero).
- `phase(name)` — mark the current phase (must be one of meta.phases).
- `log(...args)` — progress line to the user.
- `args` — the frozen value you passed as workflow_run's `args`.
- `budget` — `{total, spent(), remaining()}`; spent()/remaining() are async.
- `workflow(name, args)` — run a SAVED workflow one level deep.

### Rules

- DETERMINISM: no `Math.random()`, no `Date.now()`, no `new Date()` with no args,
  no timers — they throw. Derive everything from agent() results (this makes a
  resume replay identically at zero cost).
- Concurrency is throttled server-side (16). Fire as many promises as you like;
  the server gates them. Caps: 1000 agents/run and any USD budget you set reject
  with catchable errors (`AgentCapError`, `BudgetExceededError`).
- Write each agent prompt as a self-contained brief — the agent sees NONE of this
  conversation: restate the objective, give scope/inputs/constraints, and the
  exact output shape.

### Quality patterns (compose freely)

- Adversarial verify: every implement agent's output is checked by a separate
  agent PROMPTED TO REFUTE it; keep only what survives.
- Judge panel: N independent attempts from different angles + judge agents with a
  schema'd verdict; synthesize from the winner.
- Loop-until-dry: re-run find/fix rounds until an agent reports zero findings,
  bounded by an explicit script counter (never wall-clock).

### Approval & saving

A NEW script is shown to the user for approval before it runs (you'll get a
"ready for approval" result — don't wait, continue or end the turn). Save a
reusable workflow with `workflow_save({name, script})`; run a trusted saved one
by `workflow_run({name, args})`.
"""


def build_ultracode_directive() -> str:
    """The directive plus a live listing of the user's saved workflows so the
    model can invoke them by name (like skills)."""
    directive = _BASE
    try:
        from server.workflows import store

        saved = store.list_scripts()
    except Exception:
        saved = []
    if saved:
        lines = "\n".join(
            f"- `{s['name']}`{' (trusted)' if s.get('trusted') else ''}: {s.get('description', '')}"
            for s in saved
        )
        directive += "\n\n### Saved workflows (call by name via workflow_run)\n" + lines
    return directive
