"""The unified turn engine.

One provider-neutral agentic loop for every model family (Claude on Bedrock,
GPT on bedrock-mantle, local llama-server) and every consumer (interactive
chat, subagents, cron and headless). Providers translate their wire protocol
into neutral round events in ``events``. The engine owns round budgets,
fail-closed compaction, bounded overflow recovery, tool execution through the
shared safety gate, approval pause/resume, and cost accounting.
"""
