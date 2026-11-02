"""Shadowed permission rules: an earlier rule hides a later one only when it
applies to every tool the later one does."""

from server.security.permissions import _match_rule
from server.security.shadowed_rules import detect_shadowed_rules


def test_a_wildcard_allow_hides_a_later_deny_for_the_same_tool():
    rules = [
        {"tool": "ws_run_command", "pattern": "*", "action": "allow"},
        {"tool": "ws_run_command", "pattern": "rm *", "action": "deny"},
    ]
    [w] = detect_shadowed_rules(rules)
    assert w["shadow_type"] == "deny_shadowed_by_allow"
    assert (w["shadowed_index"], w["shadowed_by_index"]) == (1, 0)


def test_a_tool_specific_allow_does_not_hide_a_later_global_deny():
    rules = [
        {"tool": "ws_run_command", "pattern": "*", "action": "allow"},
        {"tool": "*", "pattern": "rm *", "action": "deny"},
    ]
    assert detect_shadowed_rules(rules) == []
    # The deny is reachable for every other tool.
    index, _rule = _match_rule("terminal_run", {"command": "rm x"}, rules)
    assert index == 1


def test_a_global_wildcard_hides_a_later_specific_rule():
    rules = [
        {"tool": "*", "pattern": "*", "action": "ask"},
        {"tool": "ws_write_file", "pattern": "src/*", "action": "ask"},
    ]
    [w] = detect_shadowed_rules(rules)
    assert w["shadow_type"] == "specific_shadowed_by_wildcard"
