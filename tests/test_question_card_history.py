"""A question card in the history reaches the model as text: the question,
its options and the user's answer (or that there is none). Before this, the
row reached the model as an empty assistant reply, so the next turn asked the
same question again."""

from server.infrastructure.sessions import visible_chat_history


def _card(**question):
    return {"question": "Which color do you like best?", "options": ["Blue", "Green"], **question}


def _prompt(rows):
    return visible_chat_history(rows)


def test_an_answered_card_carries_the_question_and_the_answer():
    rows = [
        {"role": "user", "content": "Ask me which color I like."},
        {
            "role": "assistant",
            "content": "",
            "userQuestions": [_card(answered=True, answer="Green")],
        },
        {"role": "user", "content": "What did I pick?"},
    ]
    card = _prompt(rows)[1]
    assert card["role"] == "assistant"
    assert "Which color do you like best?" in card["content"]
    assert "Blue; Green" in card["content"]
    assert 'The user answered: "Green"' in card["content"]


def test_a_card_with_no_answer_says_so():
    rows = [{"role": "assistant", "content": "", "userQuestions": [_card()]}]
    content = _prompt(rows)[0]["content"]
    assert "Which color do you like best?" in content
    assert "did not answer" in content
    assert "answered:" not in content


def test_the_prose_before_the_card_comes_first_and_each_question_follows():
    rows = [
        {
            "role": "assistant",
            "content": "Two quick questions.",
            "userQuestions": [
                _card(answered=True, answer="Blue"),
                {"question": "Which size?", "options": [], "answered": True, "answer": "Large"},
            ],
        }
    ]
    content = _prompt(rows)[0]["content"]
    assert content.startswith("Two quick questions.")
    assert content.index("Which color") < content.index("Which size?")
    assert 'answered: "Blue"' in content and 'answered: "Large"' in content


def test_a_row_without_cards_is_unchanged():
    row = {"role": "assistant", "content": "Plain reply."}
    assert _prompt([row])[0] is row
