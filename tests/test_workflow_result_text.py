"""A finished workflow's card used to show the LAST 2KB of a json.dumps of the
script's return value: a mid-sentence fragment, quotes as \\u2019, paragraph
breaks as a literal \\n, headline cut off. The result is now rendered as prose
and workflow cards keep the head (headline first); shell cards keep the tail
(the end of a log is what matters there)."""

from server.tasks import events as task_events
from server.workflows.manager import _render_result, _task_result_text


def _outcome(result, **extra):
    base = {"agents_spawned": 3, "cost_usd": 1.25, "tokens_in": 1000, "tokens_out": 200}
    base.update(extra)
    base["result"] = result
    return base


def test_headline_leads_and_strings_are_prose_not_json():
    text = _task_result_text("completed", _outcome("Poland’s data\n\nsecond paragraph"))
    assert text.startswith("Workflow completed: 3 agent(s), $1.25, 1200 tokens")
    # The curly apostrophe survives as itself and the paragraph break is real.
    assert "Poland’s data\n\nsecond paragraph" in text
    assert "\\u2019" not in text and "\\n" not in text


def test_nested_dict_and_list_render_as_lines():
    result = {
        "results": [
            {"id": "labour-market", "research": "line one\nline two"},
            "plain item",
        ],
        "confirmed": 4,
    }
    text = _render_result(result)
    assert "results:" in text
    assert "id: labour-market" in text
    assert "research:" in text and "line one" in text and "line two" in text
    assert "- plain item" in text
    assert "confirmed: 4" in text
    assert '"' not in text  # no JSON quoting anywhere


def test_error_path_and_empty_result_unchanged():
    assert _task_result_text("failed", _outcome(None, error="boom")).endswith("Error: boom")
    assert _task_result_text("completed", _outcome(None)) == (
        "Workflow completed: 3 agent(s), $1.25, 1200 tokens"
    )


def test_workflow_excerpt_keeps_the_head_and_shell_keeps_the_tail():
    big = "HEAD " + "x" * (task_events.RESULT_TAIL_MAX * 2) + " TAIL"
    wf = task_events._result_excerpt("workflow", big)
    sh = task_events._result_excerpt("shell", big)
    assert wf.startswith("HEAD ") and wf.endswith("…")
    assert sh.endswith(" TAIL") and not sh.startswith("HEAD")
    assert len(wf) == len(sh) == task_events.RESULT_TAIL_MAX
    # Short results pass through untouched for every kind.
    assert task_events._result_excerpt("workflow", "short") == "short"
