"""Auxiliary (side-task) model routing.

Every side call the assistant makes on the user's behalf (memory recall, the
goal evaluator, title generation, the auto-mode classifier, the permission
explainer, query rewriting, compaction summaries, the post-turn learning
review, CI diagnosis) used to hard-code ``chat_models.get("haiku")`` at its
own call site. One config map now names the model per task::

    "auxiliary_models": {
        "memory_recall": "haiku",
        "goal_evaluator": "haiku",
        "title": "haiku",
        "auto_mode_classifier": "haiku",
        "permission_explainer": "haiku",
        "query_rewrite": "haiku",
        "ci_diagnose": "haiku",
        "compaction": "main",
        "learning_review": "main"
    }

Values are ``chat_models`` keys. The special value ``"main"`` means "the
session's own model" and is resolved by the caller, which knows that model.
A task that is absent from the map keeps its historical default, so an empty
map is byte-identical to the pre-map behaviour. A local (on-device) key is
accepted wherever the call goes through ``one_shot``; the Bedrock-only call
sites (recall, classifier, explainer) fall back to their default when handed
a key that is not a cloud model.
"""

from __future__ import annotations

import logging

log = logging.getLogger("whisper-studio")

MAIN = "main"

TASKS: tuple[str, ...] = (
    "memory_recall",
    "goal_evaluator",
    "title",
    "auto_mode_classifier",
    "permission_explainer",
    "query_rewrite",
    "ci_diagnose",
    "compaction",
    "learning_review",
)


def _map(config: dict | None = None) -> dict:
    if config is None:
        try:
            from server.infrastructure.config import load_config

            config = load_config()
        except Exception:  # noqa: BLE001 - routing must never break a call site
            return {}
    raw = config.get("auxiliary_models") or {}
    return raw if isinstance(raw, dict) else {}


def aux_model_key(task: str, default: str = "haiku", *, config: dict | None = None) -> str:
    """The ``chat_models`` key configured for ``task``, else ``default``.

    Returns ``MAIN`` verbatim when the user chose "main"; callers substitute
    the session's model for it.
    """
    value = _map(config).get(task)
    if isinstance(value, str) and value.strip():
        return value.strip()
    return default


def resolve_task_key(task: str, default: str, main_model_key: str | None) -> str:
    """Like :func:`aux_model_key` but with "main" already resolved to the
    session model (or ``default`` when no session model is known)."""
    key = aux_model_key(task, default)
    if key == MAIN:
        return main_model_key or default
    return key


def aux_model_id(
    task: str,
    default_key: str = "haiku",
    *,
    fallback_id: str | None = None,
    cloud_only: bool = True,
    config: dict | None = None,
    models: dict | None = None,
) -> str | None:
    """Provider model id for ``task``: the configured key looked up in
    ``chat_models``, else the default key's id, else ``fallback_id``.

    ``models`` lets a caller that already holds a (possibly session-latched)
    ``chat_models`` map use it instead of the global catalog. ``cloud_only``
    (the default) refuses on-device ids, because the direct Bedrock call sites
    cannot use them; they get the default key's id instead.
    """
    if models is None:
        try:
            from server.chat.infra import _get_chat_models

            models = _get_chat_models()
        except Exception:  # noqa: BLE001
            models = {}
    if not isinstance(models, dict):
        models = {}
    # A rich catalog entry ({id, label, ...}) collapses to its id.
    models = {k: (v.get("id") if isinstance(v, dict) else v) for k, v in models.items()}
    key = aux_model_key(task, default_key, config=config)
    if key == MAIN:
        key = default_key
    model_id = models.get(key)
    if cloud_only and isinstance(model_id, str) and model_id.startswith("local:"):
        log.debug("auxiliary_models.%s=%r is on-device; using %r", task, key, default_key)
        model_id = models.get(default_key)
    if not model_id:
        model_id = models.get(default_key) or fallback_id
    return model_id


def aux_one_shot(
    task: str,
    system: str,
    user: str,
    *,
    max_tokens: int,
    default_key: str = "haiku",
    main_model_key: str | None = None,
) -> str:
    """Run a one-shot completion on the model configured for ``task``.

    Cloud keys (Claude or GPT on Bedrock) and on-device keys both work: the
    routing is ``server.infrastructure.oneshot.one_shot``'s. Raises on hard
    failure, exactly like ``one_shot``, so callers keep their own fallbacks.
    """
    from server.infrastructure.oneshot import one_shot
    from server.local.runtime import is_local_model

    key = resolve_task_key(task, default_key, main_model_key)
    if is_local_model(key):
        return one_shot(system, user, max_tokens=max_tokens, engine=key)
    return one_shot(system, user, max_tokens=max_tokens, engine="cloud", cloud_model_key=key)


__all__ = [
    "MAIN",
    "TASKS",
    "aux_model_id",
    "aux_model_key",
    "aux_one_shot",
    "resolve_task_key",
]
