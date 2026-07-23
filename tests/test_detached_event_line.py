"""_event_line renders the agent runtime's REAL event shape into output-file
lines. Regression: it read event/preview/tool keys the runtime never sets
(runtime emits phase/tool_name/tool_input_preview/output_preview/text), so
detached agents wrote an empty output file and task_output showed nothing.
"""

from server.tasks.agents import _event_line


def test_tool_call_line():
    assert (
        _event_line(
            {"phase": "tool_call", "tool_name": "ws_grep", "tool_input_preview": "foo", "turn": 2}
        )
        == "[tool_call] ws_grep foo"
    )


def test_tool_result_line_ok():
    assert (
        _event_line({"phase": "tool_result", "tool_name": "ws_read_file", "output_preview": "hi"})
        == "[tool_result] ws_read_file hi"
    )


def test_tool_result_line_error_prefixed():
    assert (
        _event_line(
            {
                "phase": "tool_result",
                "tool_name": "ws_read_file",
                "output_preview": "boom",
                "status": "error",
            }
        )
        == "[tool_result] ERROR ws_read_file boom"
    )


def test_text_line():
    assert _event_line({"phase": "text", "text": "thinking..."}) == "[text] thinking..."


def test_turn_start_and_completed():
    assert _event_line({"phase": "turn_start", "turn": 3}) == "[turn_start] turn 3"
    assert _event_line({"phase": "completed", "turns_used": 5}) == "[completed] turns_used=5"
    assert _event_line({"phase": "turn_limit", "turns_used": 30}) == "[turn_limit] turns_used=30"


def test_stopped_phase_rendered():
    assert _event_line({"phase": "stopped"}) == "[stopped] stopped by user"


def test_unknown_or_metadata_phases_return_empty():
    assert _event_line({"phase": "started"}) == ""
    assert _event_line({"phase": "turn_limit_unknown"}) == ""
    assert _event_line({}) == ""
