"""server.chat.repetition: detecting a repetition-dominated truncated round."""

from server.chat.repetition import MIN_FRAGMENT_LENGTH, assistant_text, is_repetition_dominated


def test_repeated_line_dominates():
    text = "\n".join(["The same sentence, repeated over and over, again and again."] * 20)
    assert is_repetition_dominated(text) is True


def test_repeated_unaligned_window_dominates():
    text = ("abcdefghij klmnopqrst uvwxyz0123 456789ABCD EFGHIJKLMN " * 12) + "tail"
    assert len(text) > MIN_FRAGMENT_LENGTH
    assert is_repetition_dominated(text) is True


def test_ordinary_prose_and_short_text_fail_open():
    prose = " ".join(f"sentence number {i} says something different each time." for i in range(30))
    assert is_repetition_dominated(prose) is False
    assert is_repetition_dominated("short " * 10) is False
    assert is_repetition_dominated(None) is False  # type: ignore[arg-type]


def test_assistant_text_flattens_text_blocks_only():
    content = [
        {"type": "thinking", "thinking": "hmm"},
        {"type": "text", "text": "hello "},
        {"type": "tool_use", "id": "t", "name": "x", "input": {}},
        {"type": "text", "text": "world"},
    ]
    assert assistant_text(content) == "hello world"
    assert assistant_text("plain") == "plain"
