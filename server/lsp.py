"""
LSP (Language Server Protocol) integration for Whisper Studio.
Provides lsp_diagnostics, lsp_hover, lsp_references tools that
query language servers for richer code understanding.
Falls back to static analysis tools when LSP servers aren't running.
"""

import logging
import os
import subprocess
import sys

from fastapi import APIRouter

log = logging.getLogger("whisper-studio")

router = APIRouter(prefix="/api/lsp", tags=["lsp"])

BASE_DIR = os.path.dirname(os.path.dirname(__file__))

LSP_TOOLS = [
    {
        "name": "lsp_diagnostics",
        "description": (
            "[LSP] Get errors, warnings, and type diagnostics for a file. "
            "Uses pyflakes/eslint depending on language. "
            "Use this to understand what's wrong before fixing code."
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


def _python_diagnostics(full_path: str) -> str:
    """Run pyflakes, falling back to a syntax check when it isn't installed."""
    try:
        result = subprocess.run(
            [sys.executable, "-m", "pyflakes", full_path],
            capture_output=True,
            text=True,
            timeout=15,
        )
        # pyflakes not installed: `python -m pyflakes` exits non-zero and prints
        # "No module named pyflakes" to stderr. That's a tool-availability error,
        # NOT a file diagnostic — returning it verbatim would tell the model the
        # file has a "No module named pyflakes" problem. Fall through to the
        # py_compile fallback instead. (pyflakes exits non-zero with real
        # findings too, so we key off the module-missing message specifically.)
        tool_missing = result.returncode != 0 and "No module named" in result.stderr
        if not tool_missing:
            output = (result.stdout + result.stderr).strip()
            return output if output else "No issues found."
    except FileNotFoundError:
        pass
    # Fallback: py_compile for syntax errors only
    try:
        result = subprocess.run(
            [sys.executable, "-m", "py_compile", full_path],
            capture_output=True,
            text=True,
            timeout=10,
        )
        if result.returncode == 0:
            return "No syntax errors."
        return result.stderr.strip() or "Syntax error detected."
    except Exception as e:
        return f"Diagnostic check failed: {e}"


def _js_diagnostics(full_path: str, ws_path: str) -> str:
    """Run ESLint for JS/TS files."""
    try:
        result = subprocess.run(
            ["npx", "--no-install", "eslint", "--format", "compact", full_path],
            capture_output=True,
            text=True,
            timeout=20,
            cwd=ws_path,
        )
        output = (result.stdout + result.stderr).strip()
        return output if output else "No ESLint issues found."
    except Exception as e:
        return f"ESLint not available ({e}). Install with: npm install -g eslint"


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
        if ext == ".py":
            return _python_diagnostics(full_path)
        elif ext in (".js", ".jsx", ".ts", ".tsx"):
            return _js_diagnostics(full_path, ws)
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


# --- API Routes ---


@router.get("/status")
async def lsp_status():
    """Check which LSP/analysis tools are available."""
    available = {}
    for tool_name, cmd in [
        ("pyflakes", ["python", "-m", "pyflakes", "--version"]),
        ("pylsp", ["pylsp", "--version"]),
        ("eslint", ["npx", "--no-install", "eslint", "--version"]),
    ]:
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=5)
            available[tool_name] = r.returncode == 0
        except Exception:
            available[tool_name] = False
    return {"tools": available, "lsp_tools": [t["name"] for t in LSP_TOOLS]}
