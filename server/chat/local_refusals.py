"""The reason a chat request that names an unknown model is refused.

A chat turn refuses a cloud model in Local mode in its own model resolution
(routes.py, through ``model_mode.turn_model_refusal``).
"""

from __future__ import annotations


def unknown_model_message(model_key: str) -> str:
    """The reason a request naming a model the catalog does not know is
    refused (it is never swapped for another model)."""
    return (
        f"{model_key} is not an available chat model (it may have been removed). "
        "Pick one from the model menu."
    )
