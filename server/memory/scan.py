"""Memory file scanning — discovers and parses memory topic files.

Walks the memory directory for .md files (excluding MEMORY.md),
extracts YAML frontmatter, and builds a manifest for LLM consumption.
"""

import logging
import os
from dataclasses import dataclass
from datetime import datetime, timezone

from server.memory.memdir import ENTRYPOINT_NAME, MEMORY_TYPES

log = logging.getLogger("whisper-studio")

MAX_MEMORY_FILES = 200
FRONTMATTER_MAX_LINES = 30


@dataclass
class MemoryFile:
    """Parsed metadata for a single memory topic file."""

    path: str  # Absolute path
    filename: str  # Relative to memory dir (e.g. "user_role.md")
    name: str  # From frontmatter
    description: str  # From frontmatter
    type: str  # user | feedback | project | reference
    mtime: float  # Modification time (epoch seconds)
    size: int  # File size in bytes


def parse_frontmatter(filepath: str) -> dict | None:
    """Extract YAML frontmatter from a memory file.

    Reads up to FRONTMATTER_MAX_LINES looking for --- delimiters.
    Returns dict with name, description, type or None on failure.
    """
    try:
        with open(filepath, encoding="utf-8") as f:
            lines = []
            for i, line in enumerate(f):
                if i >= FRONTMATTER_MAX_LINES:
                    break
                lines.append(line)
    except OSError:
        return None

    if not lines or lines[0].strip() != "---":
        return None

    # Find closing ---
    end_idx = None
    for i in range(1, len(lines)):
        if lines[i].strip() == "---":
            end_idx = i
            break

    if end_idx is None:
        return None

    # Parse key: value pairs (simple YAML, no nesting needed)
    result = {}
    for line in lines[1:end_idx]:
        line = line.strip()
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip()

    # Validate required fields
    name = result.get("name", "")
    description = result.get("description", "")
    mem_type = result.get("type", "")

    if not name:
        return None

    if mem_type and mem_type not in MEMORY_TYPES:
        mem_type = ""

    return {"name": name, "description": description, "type": mem_type}


def scan_memory_files(memory_dir: str) -> list[MemoryFile]:
    """Walk memory directory and return parsed MemoryFile entries.

    Excludes MEMORY.md and dotfiles. Sorted by mtime descending, capped at 200.
    """
    if not os.path.isdir(memory_dir):
        return []

    results = []
    for root, _dirs, files in os.walk(memory_dir):
        for fname in files:
            if not fname.endswith(".md"):
                continue
            if fname == ENTRYPOINT_NAME:
                continue
            if fname.startswith("."):
                continue

            full_path = os.path.join(root, fname)
            rel_path = os.path.relpath(full_path, memory_dir)

            try:
                stat = os.stat(full_path)
            except OSError:
                continue

            frontmatter = parse_frontmatter(full_path)
            if frontmatter is None:
                # Still include file with minimal info
                frontmatter = {"name": os.path.splitext(fname)[0], "description": "", "type": ""}

            results.append(
                MemoryFile(
                    path=full_path,
                    filename=rel_path,
                    name=frontmatter["name"],
                    description=frontmatter["description"],
                    type=frontmatter["type"],
                    mtime=stat.st_mtime,
                    size=int(stat.st_size),
                )
            )

    # Sort newest first, cap at limit
    results.sort(key=lambda m: m.mtime, reverse=True)
    return results[:MAX_MEMORY_FILES]


def build_manifest(memory_files: list[MemoryFile]) -> str:
    """Format memory files as a compact manifest for LLM consumption."""
    if not memory_files:
        return "(no memory files)"

    lines = []
    for m in memory_files:
        tag = f"[{m.type}] " if m.type else ""
        ts = datetime.fromtimestamp(m.mtime, tz=timezone.utc).strftime("%Y-%m-%d")
        desc = f": {m.description}" if m.description else ""
        lines.append(f"- {tag}{m.filename} ({ts}){desc}")
    return "\n".join(lines)
