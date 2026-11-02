"""Memory system: two-tier persistent memory with extraction and recall.

Tiers:
    global  (data/global_memory/)         cross-workspace, alive in plain chat
    project (data/memory/<slug>/)         scoped to the open workspace

init_memory() creates the data directories at startup (main.py lifespan).
"""

import logging
import os

from server.memory.memdir import GLOBAL_MEMORY_DIR, MEMORY_BASE
from server.memory.session_memory import SESSION_MEMORY_DIR

log = logging.getLogger("whisper-studio")


def init_memory() -> None:
    """Initialize memory system directories. Called once at startup."""
    os.makedirs(MEMORY_BASE, exist_ok=True)
    os.makedirs(GLOBAL_MEMORY_DIR, exist_ok=True)
    os.makedirs(SESSION_MEMORY_DIR, exist_ok=True)
    log.info("Memory system initialized: %s (global: %s)", MEMORY_BASE, GLOBAL_MEMORY_DIR)
