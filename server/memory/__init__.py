"""Memory system — two-tier persistent memory with extraction and recall.

Tiers:
    global  (data/global_memory/)       — cross-workspace, alive in plain chat
    project (data/memory/<slug>/)       — scoped to the open workspace

Public API:
    init_memory()                — Ensure data directories exist (main.py lifespan)
    MEMORY_TOOLS                 — Bedrock tool schemas for LLM
    MEMORY_TOOL_NAMES            — frozenset of tool names
    ensure_memory_dir(ws_path)   — Project memory dir if feature enabled
    ensure_global_memory_dir()   — Global memory dir if feature enabled
"""

import logging
import os

from server.memory.memdir import (
    GLOBAL_MEMORY_DIR,
    MEMORY_BASE,
    SCOPE_GLOBAL,
    SCOPE_PROJECT,
    ensure_global_memory_dir,
    ensure_memory_dir,
    get_global_memory_dir,
    get_memory_dir,
)
from server.memory.session_memory import SESSION_MEMORY_DIR
from server.memory.tools import MEMORY_TOOL_NAMES, MEMORY_TOOLS

log = logging.getLogger("whisper-studio")


def init_memory() -> None:
    """Initialize memory system directories. Called once at startup."""
    os.makedirs(MEMORY_BASE, exist_ok=True)
    os.makedirs(GLOBAL_MEMORY_DIR, exist_ok=True)
    os.makedirs(SESSION_MEMORY_DIR, exist_ok=True)
    log.info("Memory system initialized: %s (global: %s)", MEMORY_BASE, GLOBAL_MEMORY_DIR)
