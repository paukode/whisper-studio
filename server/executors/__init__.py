# Executor registry: maps executor name to a function.
# Functions receive (tool_input, transcript, current_attachments) and return a string.

EXECUTORS: dict[str, callable] = {}

# Safety metadata per executor — keyed by executor name.
# Each entry: {"read_only": bool, "concurrent_safe": bool, "destructive": bool,
#              "emits_prompt": bool}
# Defaults are fail-closed: read_only=False, concurrent_safe=False,
# destructive=False, emits_prompt=False.
EXECUTOR_META: dict[str, dict] = {}


def register_executor(
    name: str,
    *,
    read_only: bool = False,
    concurrent_safe: bool = False,
    destructive: bool = False,
    emits_prompt: bool = False,
):
    """Decorator to register a tool executor function with safety metadata.

    Args:
        name: Executor name used for dispatch.
        read_only: True if the executor only reads data (no side effects).
        concurrent_safe: True if safe to run in parallel with other tools.
        destructive: True if the executor can cause irreversible changes.
        emits_prompt: True if the result is a model PROMPT (instructions plus
            the user's input, e.g. a style hint or question wrapped around a
            transcript/document) rather than computed data. Such results must
            bypass the oversize-output budgeter: their instructions sit at the
            payload tail, so head-truncation would silently strip them and
            leave the model unable to act on what remains.

    Defaults are fail-closed: everything is assumed unsafe unless declared otherwise.
    """

    def decorator(fn):
        EXECUTORS[name] = fn
        EXECUTOR_META[name] = {
            "read_only": read_only,
            "concurrent_safe": concurrent_safe,
            "destructive": destructive,
            "emits_prompt": emits_prompt,
        }
        return fn

    return decorator


def is_concurrent_safe(executor_name: str) -> bool:
    """Check if an executor is safe for parallel execution."""
    meta = EXECUTOR_META.get(executor_name)
    if meta is None:
        return False  # fail-closed
    return meta.get("concurrent_safe", False)


def is_read_only(executor_name: str) -> bool:
    """Check if an executor is read-only."""
    meta = EXECUTOR_META.get(executor_name)
    if meta is None:
        return False
    return meta.get("read_only", False)


def is_destructive(executor_name: str) -> bool:
    """Check if an executor is destructive."""
    meta = EXECUTOR_META.get(executor_name)
    if meta is None:
        return False
    return meta.get("destructive", False)


def emits_model_prompt(executor_name: str) -> bool:
    """Check if an executor's result is a model prompt (must skip budgeting)."""
    meta = EXECUTOR_META.get(executor_name)
    if meta is None:
        return False
    return meta.get("emits_prompt", False)
