"""Boot-error registry surfaced by /health.

Boot is deliberately best-effort (a broken executor module or pricing
lookup must not take the whole app down), but swallowed failures made
"why is this tool missing?" undebuggable. Components that fail during
startup record themselves here and /health reports degraded instead of
a static ok.
"""

BOOT_ERRORS: list[dict] = []


def record_boot_error(component: str, error: str) -> None:
    BOOT_ERRORS.append({"component": component, "error": error})


def health_payload() -> dict:
    if BOOT_ERRORS:
        return {"status": "degraded", "boot_errors": list(BOOT_ERRORS)}
    return {"status": "ok"}
