"""Directory listing and filename search for the connected workspace."""

import os

from .paths import _WS_BINARY_EXTS, _WS_IGNORED_DIRS


def _ws_list_dir(root: str, rel_path: str = "") -> list[dict]:
    """List immediate children of a directory (non-recursive).

    Nothing is hidden: dot entries (.venv, .git, .env) and build folders
    (dist, node_modules) list like any other, so the file tree shows what the
    disk holds. Search and the index keep their own skip rules.

    Returns a list of entries, each with:
      - name: filename or dirname
      - path: relative path from workspace root
      - type: "file" or "directory"
      - binary: True if file has a binary extension (files only)

    A folder the OS will not let the app read raises PermissionError, so the
    caller shows the reason instead of an empty folder.
    """
    target = os.path.join(root, rel_path) if rel_path else root
    target = os.path.realpath(target)
    if not os.path.isdir(target):
        return []
    entries = []
    for name in sorted(os.listdir(target)):
        full = os.path.join(target, name)
        entry_rel = os.path.join(rel_path, name) if rel_path else name
        if os.path.isdir(full):
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
    return entries


def _ws_search_files(
    root: str, query: str, max_results: int = 100, folders: bool = False
) -> list[dict]:
    """Search for files matching a query string (case-insensitive) across the workspace.

    Walks the tree with os.walk, skipping dot folders and the build and
    dependency folders in _WS_IGNORED_DIRS, and returns the files whose path
    contains the query. With ``folders`` (the composer's @ menu), the folders
    whose path contains it come too, each ahead of its own files and marked
    ``"folder": True``. Stops after max_results matches.
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
        if folders and rel_dir and query_lower in rel_dir.lower():
            results.append({"path": rel_dir, "folder": True})
            if len(results) >= max_results:
                return results
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


# A folder attached from the @ menu lists this many entries at most.
_FOLDER_LISTING_MAX = 2000


def _ws_folder_listing(root: str, rel_path: str) -> str:
    """The text a folder attached from the composer's @ menu carries: its
    immediate entries, one a line, a folder with a trailing slash (what
    _ws_list_dir returns, so nothing is hidden)."""
    entries = _ws_list_dir(root, rel_path)
    lines = [e["name"] + ("/" if e["type"] == "directory" else "") for e in entries]
    shown = lines[:_FOLDER_LISTING_MAX]
    head = f"[Folder {rel_path}/ in the workspace: {len(lines)} entries]"
    if len(lines) > len(shown):
        head += f" (the first {len(shown)} are listed)"
    return "\n".join([head, *shown])
