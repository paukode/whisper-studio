"""Guard against tool-classification drift.

A new executor that forgets to declare read_only silently changes permission
behavior (read-only tools skip the approval pipeline entirely). This pins
that every registered executor is explicit about it.
"""

import ast
import pathlib

import server.executors.code  # noqa: F401
import server.executors.content  # noqa: F401
import server.executors.result_cache  # noqa: F401
import server.executors.terminal_run  # noqa: F401
import server.executors.web  # noqa: F401
import server.git.executor  # noqa: F401
import server.memory.executor  # noqa: F401
import server.search  # noqa: F401

# Import the registering modules exactly as server/main.py does, plus
# server.workspace which registers the ws_* executors, so the registry is
# fully populated without importing server.main (which pulls in torch).
import server.workspace  # noqa: F401
from server.executors import EXECUTOR_META, EXECUTORS

SERVER_DIR = pathlib.Path(__file__).resolve().parent.parent / "server"


def test_every_executor_has_read_only_metadata():
    for name in EXECUTORS:
        meta = EXECUTOR_META.get(name)
        assert meta is not None, f"{name} registered without metadata"
        assert isinstance(meta.get("read_only"), bool), f"{name} has no boolean read_only metadata"


def test_register_calls_declare_read_only_explicitly():
    # register_executor defaults read_only=False, so the runtime registry
    # cannot tell "explicit False" from "forgot to classify". Enforce
    # explicitness at the source level instead.
    missing = []
    for path in SERVER_DIR.rglob("*.py"):
        if "__pycache__" in path.parts:
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            name = getattr(func, "id", None) or getattr(func, "attr", None)
            if name != "register_executor" or not node.args:
                continue
            if not any(kw.arg == "read_only" for kw in node.keywords):
                missing.append(f"{path.relative_to(SERVER_DIR)}:{node.lineno}")
    assert not missing, (
        "register_executor calls without an explicit read_only keyword: " + ", ".join(missing)
    )
