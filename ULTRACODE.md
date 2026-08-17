# Ultracode: reviewing a repo with a background agent swarm

Ultracode is an effort level that also unlocks the workflow runtime. Instead of
grinding through work sequentially in one chat context, the assistant writes a
small JavaScript orchestration script and launches it **detached**: agents run
16 at a time, up to 1000 per run, and your chat session is free again the moment
the launching turn ends.

This is the walkthrough for the most common case: you cloned a repo and want it
reviewed properly.

## Setup

1. **Pick a model with Ultracode.** Click the model chip in the composer. Opus 5,
   Sonnet 5, Fable and the GPT-5.x models expose Ultracode. Haiku has no effort
   levels at all, and the pre-5 models (Sonnet 4.5, Opus 4.7 and below) top out
   at Max.

   Ultracode is orchestration, not a reasoning value, so it does not require a
   model with the deepest reasoning rung — it rides whatever each model's top
   rung is. That is why Sonnet 5 has it even though its ladder stops one step
   below Opus's.
2. **Set the effort chip to Ultracode.** This is the switch. Nothing else in the
   UI turns workflows on.
3. **Connect the workspace.** Workspace chip, then pick the cloned folder.

That is the whole setup. Whether a launch needs your approval depends on your
permission mode (Settings, Keys and permissions):

| Mode for the `workflow` category | What happens on launch |
| --- | --- |
| `auto` (the default) | Runs immediately, no card, while the run's budget is within 600k output tokens |
| `bypassPermissions` | Runs immediately, any budget |
| anything else | Shows a preview card with the full script; you click Run |
| `dontAsk` | Declines, and tells the assistant why |

## Ask for the review

Set Ultracode, then ask for something worth fanning out. Scope and shape matter
far more than length:

> Review this repo end to end. Fan out: one agent per subsystem for correctness,
> one pass for security, one for test coverage, one for dead code. Have every
> finding checked by a separate agent told to refute it, and drop anything that
> does not survive. Be exhaustive, do not sample. Give me the surviving findings
> ranked by severity with file:line.

Three things make that prompt work:

- **Name the dimensions.** They become the parallel branches of the script.
- **Ask for adversarial verification.** The runtime is built for it, and it is
  what keeps a large fan-out from returning plausible noise.
- **Say "be exhaustive".** Ultracode's own instructions say token cost is not a
  constraint, so depth is yours to ask for.

Runs are capped at 600k agent output tokens by default. The assistant can raise
that, but a bigger budget is exactly what the approval card exists for, so
expect one click if you ask for something enormous.

## What you will see

1. The assistant thinks, writes the script, and calls `workflow_run`.
2. A workflow card appears in the chat with the run id, its phases, live agent
   count and running cost.
3. **The turn ends there.** The assistant does not sit and wait for the swarm.

Watch it from two places:

- **The chat card**, live, with a Stop button.
- **The Background Tasks panel**, alongside detached agents and shell commands.

## Keep chatting while it runs

Send your next message straight away. The swarm is not a chat turn: the stream
slot is released when the launching turn finishes, so the composer is live again
after a few seconds while the run keeps going for however long it takes.

When it finishes you get a task card in the session, and the assistant is told
the outcome on your next message, so you can simply ask "what did the review
find?" and it will know.

## Reusing a workflow

Ask the assistant to save the script (`workflow_save`). Approving a workflow's
preview card is what trusts it: a saved script you have approved runs without
a preview from then on, including via the slash command:

```
/workflow repo-review
```

## Limits and controls

- 16 agents run concurrently; queue as many as you like beyond that. That is a
  real 16, not just 16 scheduled: the model-call pool is sized to the same
  number, and the Bedrock client uses adaptive retries so a burst that trips
  your account's quota is paced rather than failed.
- 1000 agents per run, 600k output tokens by default. Both raise catchable
  errors inside the script rather than dying silently.
- Stop a run from its card in chat.
- Workflows do not nest more than one level: a script can call a saved workflow,
  but that workflow cannot call another.

## If it does not fan out

- **You got one agent instead of a swarm.** Check the effort chip actually says
  Ultracode. Any other level, and the workflow tools are not offered to the
  model at all.
- **A preview card on the first run.** Either the run's budget is above 600k,
  or the `workflow` category is not on `auto`. Approving the card covers the
  rest of the session for that exact script, and trusts a saved workflow
  durably — one approval, no repeat cards.
- **Nothing about workflows in Settings.** By design: approval and management
  happen in chat (ask the assistant, or `workflow_list` / `workflow_delete`).
  Runs live on their chat card and in the Background Tasks panel.
