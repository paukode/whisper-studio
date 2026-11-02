"""The ultracode directive: teaches the workflow runtime (WS-D).

In ultracode mode the model authors a deterministic JS orchestration script and
runs it with ``workflow_run``. A locked-down Node harness executes it detached,
proxying every ``agent()`` back through the provider adapter with 16-way
concurrency, a 1000-agent cap, budgets, and a resumable journal.
"""

from __future__ import annotations

_BASE = """

## Ultracode mode: the workflow runtime

Ultracode is active: optimise for the most thorough, correct result; token cost
is not a constraint. For substantive, multi-part, or long-horizon work, do not
grind sequentially in one context. Write a **workflow script** and run it with
`workflow_run`. It runs detached and you get a run_id back. Do not poll it: end
the turn or do other work, and the run's completion wakes you. To block until
it ends, call `workflow_wait` (or `task_wait` for detached agents). Never
sleep-poll with `sleep` and `workflow_status`. For trivial or conversational
turns, just answer directly.

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
// One agent per independent piece of work, each on its own files.
const results = await parallel(targets.map(t => () =>
  agent(`Fix ${t} given: ${findings.text}. Change only ${t}.`,
        { label: `fix:${t}`, phase: "implement", schema: FIX_SCHEMA })));

phase("verify");
const verdicts = await pipeline(results.filter(Boolean),
  r => agent(`Adversarially verify this fix, try to REFUTE it: ${JSON.stringify(r.output)}`,
             { phase: "verify" }));

const partial = results.filter(r => r && r.status === "incomplete");
return { fixed: results.filter(r => r && r.status === "completed").length,
         partial: partial.map(r => r.agent_id), verdicts,
         deliverables: ["/abs/path/report.docx"] };  // files this run produced
```

### API (all injected as globals)

- `agent(prompt, opts?) -> {text, output, usage, status, agent_id, stop_reason}`:
  spawn a subagent. `opts`: `label`, `phase`, `schema` (JSON Schema, then
  `output` is the validated object, else null), `effort` (low|medium|high|extra|
  max; omit to inherit the session's level, which is the right default; any
  value is clamped to what the agent's model supports), `isolation:"worktree"`
  (an isolated git worktree for a writer), `agentType`, `model`. `status` is
  `"completed"` (finished), `"incomplete"` (a round, time or cost limit stopped
  it: `text` is partial and `stop_reason` names the limit) or `"failed"`. An
  agent never throws for its own failure: the script decides what to do.
- `pipeline(items, ...stages)`: run each item through ALL stages independently,
  with NO barrier between stages (item A can be in stage 3 while B is in stage
  1). Each stage is `(prev, item, index) => Promise`. A stage that throws drops
  that item to null and logs the error. This is the DEFAULT for multi-stage work.
- `parallel(thunks)`: a BARRIER that awaits all thunks. A thunk that throws
  becomes null (the error is logged), so `.filter(Boolean)` before using. Use
  only when you need ALL results at once (dedup/merge across the full set,
  early-exit on zero). In both, `AgentCapError`, `BudgetExceededError` and a
  cancel are not dropped: they reject the whole call.
- `phase(name)`: mark the current phase (must be one of meta.phases).
- `log(...args)`: a progress line to the user.
- `args`: the frozen value you passed as workflow_run's `args`.
- `budget`: `{total, spent(), remaining()}` in agent OUTPUT tokens;
  spent()/remaining() are async.
- `workflow(name, args)`: run a SAVED workflow one level deep.
- `tools.<name>(args)`: call one read-only tool, for example
  `tools.workflow_status({run_id})` or `tools.task_status({task_id})`.

### Sizing the run

- One agent per independent piece of work. Count the independent pieces the
  task really has and spawn that many, up to 16 at once (the server runs 16 at
  a time). Never use a fixed count, and never pad with extra agents that
  repeat each other's work.
- Writers get file-disjoint work: no two agents edit the same file. When the
  work cannot be split by file, give each writer `isolation:"worktree"`.
- Every change gets a checker: a separate agent that verifies it (runs the
  tests, reads the diff, tries to refute it).
- Each agent has its own limits from Settings: {round_limit} rounds and
  {time_limit} minutes. Size each agent's task for well under that, so it
  finishes. An agent that hits a limit returns `status:"incomplete"`: treat
  its result as partial and run a new agent for the remainder.
- Do not set `opts.model` unless the user asked for a model. Agents use the
  model the user picked in the chat's model menu.

### Rules

- DETERMINISM: no `Math.random()`, no `Date.now()`, no `new Date()` with no args,
  no timers: they throw. Derive everything from agent() results (this makes a
  resume replay identically at zero cost). A resume replays only completed
  agents; incomplete and failed agents run again.
- Caps: 1000 agents/run and the output-token budget (600000 by default, at
  least 100000; override with workflow_run's `budget_tokens`) reject with
  catchable errors (`AgentCapError`, `BudgetExceededError`).
- Write each agent prompt as a self-contained brief. The agent sees NONE of this
  conversation: restate the objective, give scope/inputs/constraints, and the
  exact output shape.
- DELIVERABLES: if the run produces files, return their paths in
  `deliverables` (absolute, or relative to the workspace). The server checks
  each one exists and is non-empty BEFORE the run counts as completed; a
  missing file fails the run with an error naming it. Never list a file an
  agent merely said it wrote: take the path from a tool result that saved it.

### Quality patterns (compose freely)

- Adversarial verify: every implement agent's output is checked by a separate
  agent PROMPTED TO REFUTE it; keep only what survives.
- Judge panel: independent attempts from different angles plus judge agents
  with a schema'd verdict; synthesize from the winner.
- Loop-until-dry: re-run find/fix rounds until an agent reports zero findings,
  bounded by an explicit script counter (never wall-clock).

### Approval and saving

A NEW script is shown to the user for approval before it runs (you get a
"ready for approval" result: do not wait, continue or end the turn). If the
user's workflow permission mode auto-approves it, the result carries
`auto_approved: true` and a run_id instead. Save a reusable workflow with
`workflow_save({name, script})`; run a trusted saved one by
`workflow_run({name, args})`. Approving the preview card is what trusts a
workflow: a saved script the user approved runs by name with no card, and
`workflow_delete({name})` removes one from the library.
"""


def _limits_text() -> tuple[str, str]:
    """The live round and time limits every agent runs with (Settings >
    Costs > Budget), as the directive states them."""
    from server.infrastructure import run_limits

    minutes = run_limits.time_limit_seconds() / 60.0
    shown = str(int(minutes)) if minutes == int(minutes) else f"{minutes:.1f}"
    return str(run_limits.round_limit()), shown


def build_ultracode_directive() -> str:
    """The directive, with the live agent limits from Settings, plus a listing
    of the user's saved workflows so the model can invoke them by name (like
    skills). It sits on the dynamic prompt layer, so a Settings change shows
    in the next request."""
    from server.workflows import store

    rounds, minutes = _limits_text()
    directive = _BASE.replace("{round_limit}", rounds).replace("{time_limit}", minutes)
    saved = store.list_scripts()
    if saved:
        lines = "\n".join(
            f"- `{s['name']}`{' (trusted)' if s.get('trusted') else ''}: {s.get('description', '')}"
            for s in saved
        )
        directive += "\n\n### Saved workflows (call by name via workflow_run)\n" + lines
    return directive
