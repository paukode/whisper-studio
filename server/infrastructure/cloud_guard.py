"""Local mode keeps every model call on this Mac.

The first-run notice and the Settings panel promise that in Local mode nothing
leaves the Mac. Every caller that would reach a cloud service directly
(Bedrock InvokeModel, the mantle Responses API, the Nova Sonic stream) asks
this module first, so the promise is enforced in one place instead of each
caller deciding for itself.

The guard refuses; it never reroutes. A caller that has an on-device path
takes it explicitly (OCR keeps Apple Vision, compaction summarises on the
session's own on-device model); a caller without one hands the refusal reason
to the user. Hybrid and Cloud modes allow cloud calls.

Imports stay lazy and never go through ``server.chat``, so any layer
(including ``server.infrastructure``) can use it.
"""

from __future__ import annotations

SETTINGS_HINT = "Switch to Hybrid or Cloud in Settings > Model mode to use it."


class CloudRefused(RuntimeError):
    """A cloud call refused because the app is in Local mode.

    ``str(exc)`` is the user-facing reason; ``feature`` names what was refused.
    """

    def __init__(self, feature: str, service: str = "Amazon Bedrock") -> None:
        self.feature = feature
        super().__init__(refusal_reason(feature, service))


def refusal_reason(feature: str, service: str = "Amazon Bedrock") -> str:
    """The one sentence every Local-mode refusal shows the user."""
    return f"{feature} uses {service}, and Local mode keeps everything on this Mac. {SETTINGS_HINT}"


def cloud_allowed(config: dict | None = None) -> bool:
    """False in Local mode, where no call may leave this Mac."""
    from server.infrastructure.model_mode import current_mode

    return current_mode(config) != "local"


def cloud_refusal(
    feature: str, config: dict | None = None, *, service: str = "Amazon Bedrock"
) -> str | None:
    """The user-facing reason ``feature`` may not call the cloud, or None when
    it may."""
    return None if cloud_allowed(config) else refusal_reason(feature, service)


def require_cloud(
    feature: str, config: dict | None = None, *, service: str = "Amazon Bedrock"
) -> None:
    """Raise :class:`CloudRefused` in Local mode; return quietly otherwise."""
    if not cloud_allowed(config):
        raise CloudRefused(feature, service)


__all__ = [
    "SETTINGS_HINT",
    "CloudRefused",
    "cloud_allowed",
    "cloud_refusal",
    "refusal_reason",
    "require_cloud",
]
