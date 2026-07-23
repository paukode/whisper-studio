"""Module-level registry of ApprovalSpec instances keyed by tool/action name.

The hybrid model: tool modules `register(...)` next to their definitions
(declared at tool); this module is the central lookup (validated/audited
centrally). Importing the tool module is what populates the registry —
import order matters in the sense that the bootstrap (`server/approval/
bootstrap.py`) must be imported before any code calls `get()`.
"""

from __future__ import annotations

import inspect

from .spec import ApprovalOutcome, ApprovalSpec, Executor

_REGISTRY: dict[str, ApprovalSpec] = {}


def _wrap_executor(fn) -> Executor:
    """Ensure executor is async even if the caller registered a sync fn."""
    if inspect.iscoroutinefunction(fn):
        return fn

    async def _wrapped(payload: dict) -> ApprovalOutcome:
        result = fn(payload)
        if inspect.isawaitable(result):
            return await result
        return result

    return _wrapped


def register(action: str, spec: ApprovalSpec) -> None:
    """Register a spec for an action/tool name. Idempotent — later calls
    overwrite earlier ones, useful during dev reloads."""
    spec.executor = _wrap_executor(spec.executor)
    _REGISTRY[action] = spec


def get(action: str) -> ApprovalSpec | None:
    return _REGISTRY.get(action)


def all_specs() -> dict[str, ApprovalSpec]:
    return dict(_REGISTRY)


def all_categories() -> list[str]:
    seen: list[str] = []
    for spec in _REGISTRY.values():
        if spec.category not in seen:
            seen.append(spec.category)
    return seen


__all__ = ["register", "get", "all_specs", "all_categories", "_wrap_executor"]
