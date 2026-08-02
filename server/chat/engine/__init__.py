"""The unified turn engine.

One provider-neutral agentic loop for every model family (Claude on Bedrock,
GPT on bedrock-mantle, local llama-server) and every consumer (interactive
chat, subagents, cron). Providers reduce to thin adapters that translate the
wire protocol into the neutral round events in ``events``; the engine owns
everything that must behave identically: round budgets, context management
(compaction, prompt-too-long rescue, the salvage round), tool execution
through the shared safety gate, approval pause/resume, and cost accounting.

P1 (this package's first slice) ships the shared vocabulary only:
``events`` and ``windows``. The loop itself lands in P2.
"""
