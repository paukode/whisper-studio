"""Whisper Workflow Runtime.

The model writes a deterministic JS orchestration script; a locked-down Node 22
harness executes it detached from the chat turn, proxying every ``agent()`` call
back over stdio JSON-RPC into the Python server (both providers).
Server-enforced 16-way concurrency, a 1000-agent lifetime cap, token budgets, a
resumable journal, and an upfront phase-preview approval.

Modules: rpc/journal/store (foundations), harness/* (Node),
runtime/agent_adapter/manager (execution), tools/routes (exposure).
"""
