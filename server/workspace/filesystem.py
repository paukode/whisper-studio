"""Directory listing and filename search for the connected workspace."""

import os

from .paths import _WS_BINARY_EXTS, _WS_IGNORED_DIRS


def _ws_list_dir(root: str, rel_path: str = "") -> list[dict]:
    """List immediate children of a directory (non-recursive).

    Returns a list of entries, each with:
      - name: filename or dirname
      - path: relative path from workspace root
      - type: "file" or "directory"
      - binary: True if file has a binary extension (files only)
    """
    target = os.path.join(root, rel_path) if rel_path else root
    target = os.path.realpath(target)
    if not os.path.isdir(target):
        return []
    entries = []
    try:
        for name in sorted(os.listdir(target)):
            if name.startswith(".") or name == "Thumbs.db":
                continue
            full = os.path.join(target, name)
            entry_rel = os.path.join(rel_path, name) if rel_path else name
            if os.path.isdir(full):
                if name in _WS_IGNORED_DIRS:
                    continue
                entries.append({"name": name, "path": entry_rel, "type": "directory"})
            elif os.path.isfile(full):
                ext = os.path.splitext(name)[1].lower()
                entries.append(
                    {
                        "name": name,
                        "path": entry_rel,
                        "type": "file",
                        "binary": ext in _WS_BINARY_EXTS,
                    }
                )
    except PermissionError:
        pass
    return entries


def _ws_search_files(root: str, query: str, max_results: int = 100) -> list[dict]:
    """Search for files matching a query string (case-insensitive) across the workspace.

    Uses os.walk with the same ignore rules as the old scan, but returns only
    files whose path contains the query. Stops after max_results matches.
    """
    query_lower = query.lower()
    results = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(
            d for d in dirnames if d not in _WS_IGNORED_DIRS and not d.startswith(".")
        )
        rel_dir = os.path.relpath(dirpath, root)
        if rel_dir == ".":
            rel_dir = ""
        for fname in sorted(filenames):
            if fname.startswith(".") or fname == "Thumbs.db":
                continue
            rel_path = os.path.join(rel_dir, fname) if rel_dir else fname
            if query_lower in rel_path.lower():
                ext = os.path.splitext(fname)[1].lower()
                results.append({"path": rel_path, "binary": ext in _WS_BINARY_EXTS})
                if len(results) >= max_results:
                    return results
    return results
