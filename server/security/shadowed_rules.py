"""
Shadowed rule detection for Whisper Studio.

Detects unreachable permission rules in a rule list so users can fix
config mistakes before they cause unexpected behaviour.

Two shadow types:
  deny_shadowed_by_allow    — a broad allow rule fires before a specific deny rule,
                              making the deny unreachable.
  specific_shadowed_by_wildcard — a specific allow/ask rule follows a wildcard rule
                              for the same tool and action, making the specific rule
                              unreachable.
"""

from __future__ import annotations


def detect_shadowed_rules(rules: list[dict]) -> list[dict]:
    """
    Analyse a rule list for unreachable (shadowed) entries.

    Returns a list of warning dicts:
        {
          "shadowed_index":    int,   # index of the shadowed rule
          "shadowed_by_index": int,   # index of the rule that shadows it
          "shadow_type":       str,   # "deny_shadowed_by_allow" | "specific_shadowed_by_wildcard"
          "tool":              str,
          "suggestion":        str,
        }
    """
    warnings: list[dict] = []

    for i, rule_i in enumerate(rules):
        tool_i = rule_i.get("tool", "")
        action_i = rule_i.get("action", "")
        pattern_i = rule_i.get("pattern", rule_i.get("prefix", "*"))

        for j, rule_j in enumerate(rules):
            if j >= i:
                # Only earlier rules (j < i) can shadow later ones (i)
                break

            tool_j = rule_j.get("tool", "")
            action_j = rule_j.get("action", "")
            pattern_j = rule_j.get("pattern", rule_j.get("prefix", "*"))

            # Rules only interact when they apply to the same tool (or one is wildcard *)
            if not _tools_overlap(tool_i, tool_j):
                continue

            # Type 1: deny_shadowed_by_allow
            # Earlier rule j is "allow" with wildcard, later rule i is "deny" with specific pattern
            if (
                action_j == "allow"
                and action_i == "deny"
                and _is_wildcard(pattern_j)
                and not _is_wildcard(pattern_i)
            ):
                warnings.append(
                    {
                        "shadowed_index": i,
                        "shadowed_by_index": j,
                        "shadow_type": "deny_shadowed_by_allow",
                        "tool": tool_i,
                        "suggestion": (
                            f"Move the deny rule (index {i}) before the allow rule (index {j}), "
                            "or narrow the allow rule's pattern so the deny can match first."
                        ),
                    }
                )
                break

            # Type 2: specific_shadowed_by_wildcard
            # Earlier rule j has the same action and a wildcard; later rule i is more specific
            if action_j == action_i and _is_wildcard(pattern_j) and not _is_wildcard(pattern_i):
                warnings.append(
                    {
                        "shadowed_index": i,
                        "shadowed_by_index": j,
                        "shadow_type": "specific_shadowed_by_wildcard",
                        "tool": tool_i,
                        "suggestion": (
                            f"Move the specific rule (index {i}) before the wildcard rule (index {j}), "
                            "or remove the wildcard rule if the specific rule should be the only match."
                        ),
                    }
                )
                break

    return warnings


def _tools_overlap(tool_a: str, tool_b: str) -> bool:
    """Two rules overlap when they share a tool name or either is the global wildcard."""
    return tool_a == tool_b or tool_a == "*" or tool_b == "*"


def _is_wildcard(pattern: str) -> bool:
    """A pattern is a wildcard when it matches everything (bare * or empty)."""
    return not pattern or pattern == "*"
