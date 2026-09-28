"""
Code-intelligence tools for the assistant: lsp_diagnostics, lsp_hover and
lsp_references.

Diagnostics run ruff on Python and the workspace's own ESLint on JS/TS, with
the commands server/code_tools resolves (the same ones Settings > Code tools
reports). Hover shows the symbol and its context, and references are a
bounded workspace word search; neither talks to a language server.
"""

import os

from server.code_tools import eslint, ruff
from server.code_tools.commands import JS_EXTENSIONS, PYTHON_EXTENSIONS

LSP_TOOLS = [
    {
        "name": "lsp_diagnostics",
        "description": (
            "[LSP] Lint a workspace file and return its errors and warnings: ruff for "
            "Python (the project's ruff config applies), the workspace's own ESLint for "
            "JS/TS. Use this to understand what's wrong before fixing code."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path in the workspace"},
            },
            "required": ["path"],
        },
    },
    {
        "name": "lsp_hover",
        "description": (
            "[LSP] Get hover info (type annotations, documentation) for a symbol "
            "at a specific line and column in a file."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path"},
                "line": {"type": "integer", "description": "1-based line number"},
                "column": {"type": "integer", "description": "0-based column offset"},
            },
            "required": ["path", "line", "column"],
        },
    },
    {
        "name": "lsp_references",
        "description": "[LSP] Find all usages/references of a symbol at a specific location.",
        "input_schema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Relative file path"},
                "line": {"type": "integer", "description": "1-based line number"},
                "column": {"type": "integer", "description": "0-based column offset"},
            },
            "required": ["path", "line", "column"],
        },
    },
]


def _extract_symbol(line: str, column: int) -> str:
    """Extract the identifier at a given column."""
    if column >= len(line):
        return ""
    start = column
    while start > 0 and (line[start - 1].isalnum() or line[start - 1] == "_"):
        start -= 1
    end = column
    while end < len(line) and (line[end].isalnum() or line[end] == "_"):
        end += 1
    return line[start:end]


def _grep_references(symbol: str, ws_path: str) -> str:
    """Find all references to a symbol via grep across the workspace."""
    if not symbol:
        return "No symbol found at that position."
    import re

    IGNORED = {".git", "node_modules", "__pycache__", "venv", ".venv"}
    TEXT_EXTS = {
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".java",
        ".go",
        ".rs",
        ".rb",
        ".cpp",
        ".c",
        ".h",
    }
    matches = []
    for dirpath, dirnames, filenames in os.walk(ws_path):
        dirnames[:] = [d for d in dirnames if d not in IGNORED and not d.startswith(".")]
        for fname in filenames:
            ext = os.path.splitext(fname)[1].lower()
            if ext not in TEXT_EXTS:
                continue
            full = os.path.join(dirpath, fname)
            rel = os.path.relpath(full, ws_path)
            try:
                with open(full, errors="replace") as f:
                    for i, line in enumerate(f, 1):
                        if re.search(r"\b" + re.escape(symbol) + r"\b", line):
                            matches.append(f"{rel}:{i}: {line.rstrip()}")
                            if len(matches) >= 100:
                                return "\n".join(matches) + "\n... (100 match limit)"
            except Exception:
                continue
    return "\n".join(matches) if matches else f"No references to '{symbol}' found."


def execute_lsp_tool(tool_name: str, tool_input: dict) -> str:
    from server.workspace import _ws_validate_path, get_workspace_path

    ws = get_workspace_path()
    if not ws:
        return "No workspace connected."

    path = tool_input.get("path", "")
    full_path = os.path.join(ws, path)

    if not _ws_validate_path(full_path, ws) or not os.path.isfile(full_path):
        return f"File not found: {path}"

    ext = os.path.splitext(path)[1].lower()

    if tool_name == "lsp_diagnostics":
        if ext in PYTHON_EXTENSIONS:
            return ruff.check_file(ws, full_path, path)
        elif ext in JS_EXTENSIONS:
            return eslint.check_file(ws, full_path, path)
        else:
            size = os.path.getsize(full_path)
            return f"No LSP configured for {ext} files. File: {path} ({size} bytes, readable)."

    elif tool_name in ("lsp_hover", "lsp_references"):
        line_no = tool_input.get("line", 1)
        column = tool_input.get("column", 0)
        try:
            with open(full_path, errors="replace") as f:
                lines = f.readlines()
            if line_no < 1 or line_no > len(lines):
                return f"Line {line_no} out of range (file has {len(lines)} lines)"
            target_line = lines[line_no - 1].rstrip()
            symbol = _extract_symbol(target_line, column)
            ctx_start = max(0, line_no - 3)
            ctx_end = min(len(lines), line_no + 2)
            context = "\n".join(
                f"{i + 1:>4}: {ln.rstrip()}"
                for i, ln in enumerate(lines[ctx_start:ctx_end], ctx_start)
            )
            if tool_name == "lsp_hover":
                return (
                    f"File: {path}  Line: {line_no}  Column: {column}\n"
                    f"Symbol: {symbol!r}\n\nContext:\n{context}\n\n"
                    f"[Full hover docs require a running language server. "
                    f"For Python: pip install python-lsp-server]"
                )
            else:
                refs = _grep_references(symbol, ws)
                return f"References to '{symbol}':\n{refs}"
        except Exception as e:
            return f"Error: {e}"

    return f"Unknown LSP tool: {tool_name}"
