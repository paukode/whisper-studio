"""Strict tool arguments and the schemas available when a round was issued.

Validation never supplies defaults, coerces values, or retrieves remote schema
references. ToolCatalog carries execution-only metadata without changing the
advertised list or its serialized prompt-cache bytes.
"""

from __future__ import annotations

import copy
import json
import math
import re

from jsonschema import ValidationError
from jsonschema.validators import validator_for
from referencing import Registry


class ToolInputError(ValueError):
    """An unsafe or ambiguous call, refused before tool side effects."""


def _label(value) -> str:
    """A bounded identifier for diagnostics, not arbitrary model output."""
    if not isinstance(value, str):
        return "unknown"
    return re.sub(r"[^A-Za-z0-9_.:/-]", "?", value[:80]) or "unknown"


class ToolCatalog(list):
    """Advertised tools, with the same assembly's full available definitions."""

    def __init__(self, offered: list[dict], *, available: list[dict]):
        super().__init__(offered)
        self.available = available


def schemas_for_tools(tools: list[dict]) -> dict:
    """Freeze this assembly's schemas, including entitled deferred tools.

    A plain list is a complete catalog. Offered definitions take precedence
    over deferred ones, so the exact schema sent to the provider always wins.
    """
    available = tools.available if isinstance(tools, ToolCatalog) else []
    return copy.deepcopy({t["name"]: t.get("input_schema") for t in [*available, *tools]})


def _unique_object(pairs: list[tuple]) -> dict:
    result = {}
    for key, value in pairs:
        if key in result:
            raise ToolInputError("Duplicate JSON object keys are ambiguous.")
        result[key] = value
    return result


def _invalid_constant(value: str):
    raise ToolInputError("Non-finite numbers are not JSON values.")


def _require_json(value, ancestors: set[int] | None = None) -> None:
    """Reject Python values JSON encoding would silently change or cannot store."""
    kind = type(value)
    if value is None or kind in (str, bool, int):
        return
    if kind is float:
        if not math.isfinite(value):
            raise ToolInputError("Arguments contain a non-finite number.")
        return
    if kind not in (dict, list):
        raise ToolInputError("Arguments contain a value that is not JSON.")
    if ancestors is None:
        ancestors = set()
    identity = id(value)
    if identity in ancestors:
        raise ToolInputError("Arguments contain a circular reference.")
    ancestors.add(identity)
    try:
        if kind is dict:
            if any(type(key) is not str for key in value):
                raise ToolInputError("JSON object keys must be strings.")
            children = value.values()
        else:
            children = value
        for child in children:
            _require_json(child, ancestors)
    finally:
        ancestors.remove(identity)


def parse_tool_arguments(raw, name: str) -> dict:
    """Parse an explicit, complete JSON object, never an empty-input fallback."""
    label = _label(name)
    if not isinstance(raw, str) or not raw.strip():
        raise ToolInputError(
            f"Tool '{label}' has missing or blank arguments; expected a JSON object."
        )
    try:
        value = json.loads(raw, object_pairs_hook=_unique_object, parse_constant=_invalid_constant)
        _require_json(value)
    except (ValueError, RecursionError):
        raise ToolInputError(f"Tool '{label}' has malformed JSON arguments.") from None
    if not isinstance(value, dict):
        raise ToolInputError(f"Tool '{label}' arguments must be a JSON object.")
    return value


def validate_call_ids(calls: list[dict]) -> None:
    """Reject missing identities and replay within a single argument round."""
    seen = set()
    for call in calls:
        if not isinstance(call, dict):
            raise ToolInputError("Tool call must be an object.")
        tid, name = call.get("id"), call.get("name")
        if not isinstance(tid, str) or not tid.strip():
            raise ToolInputError("Tool call has a missing id.")
        if tid in seen:
            raise ToolInputError("Duplicate tool call id in this round.")
        seen.add(tid)
        if not isinstance(name, str) or not name.strip():
            raise ToolInputError("Tool call has a missing name.")


def validate_tool_input(name: str, value, schemas: dict) -> None:
    """Validate unmodified model or hook input against its declared JSON Schema.

    Required on every execution path. A missing, invalid or unresolvable
    schema is a refusal, not permission to execute without validation.
    Internal execution context is stamped only AFTER this check.
    """
    label = _label(name)
    if not isinstance(value, dict):
        raise ToolInputError(f"Tool '{label}' arguments must be a JSON object.")
    if name not in schemas:
        raise ToolInputError(f"Unknown tool '{label}': no schema was available for this call.")
    schema = schemas[name]
    try:
        _require_json(value)
        if not isinstance(schema, (dict, bool)):
            raise ValueError("missing JSON Schema")
        _require_json(schema)
        cls = (
            validator_for(schema, default=None)
            if isinstance(schema, dict) and "$schema" in schema
            else validator_for(schema)
        )
        if cls is None:
            raise ValueError("unsupported JSON Schema dialect")
        cls.check_schema(schema)
        cls(schema, registry=Registry()).validate(value)
    except ValidationError as exc:
        # ValidationError.message and instance paths can contain user data.
        # Use only the bounded schema keyword, never argument values or keys.
        keyword = _label(exc.validator)
        raise ToolInputError(
            f"Tool '{label}' arguments do not match its schema ({keyword} constraint)."
        ) from None
    except Exception as exc:
        raise ToolInputError(
            f"Tool '{label}' arguments or schema could not be validated ({type(exc).__name__})."
        ) from None
