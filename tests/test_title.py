"""Tests for the deterministic cleanup applied to generated session titles."""

import pytest

from server.chat.routes import _clean_title


@pytest.mark.parametrize(
    "raw, expected",
    [
        ('"Cron Job Fix"', "Cron Job Fix"),  # strip surrounding quotes
        ("'Weekly Report'", "Weekly Report"),  # strip single quotes
        ("Title: Weekly Report", "Weekly Report"),  # drop a leading "Title:"
        ("Foo — Bar – Baz", "Foo Bar Baz"),  # em/en dashes removed
        ("one two three four five six seven", "one two three four five six"),  # clamp to 6 words
        ("Trailing punctuation here.", "Trailing punctuation here"),  # trailing punct
        ("  spaced   out   title  ", "spaced out title"),  # collapse whitespace
        ("", "New Conversation"),  # empty falls back
        ("   ", "New Conversation"),  # whitespace falls back
        # Pure-words rules (Q3-style labels kept; dates / symbols dropped):
        ("Q3 Marketing Budget Plan", "Q3 Marketing Budget Plan"),  # keep alphanumeric label
        ("S3 Bucket Cleanup", "S3 Bucket Cleanup"),  # keep alphanumeric label
        ("Meeting Notes July 2026", "Meeting Notes"),  # drop month + year
        ("Release 2026-07-05 Summary", "Release Summary"),  # drop ISO date
        ("Standup 7/6 Recap", "Standup Recap"),  # drop slash date
        ("Budget & Timeline Review", "Budget and Timeline Review"),  # & becomes "and"
        ("Sprint Retro on Monday", "Sprint Retro on"),  # drop weekday name
        ("Fix #42 in Parser", "Fix 42 in Parser"),  # hashtag symbol dropped, 42 kept
        ("Launch on 6th of the Month", "Launch on of the Month"),  # drop ordinal date
    ],
)
def test_clean_title(raw, expected):
    assert _clean_title(raw) == expected


def test_clean_title_at_most_six_words():
    out = _clean_title("alpha beta gamma delta epsilon zeta eta theta")
    assert len(out.split()) == 6


def test_clean_title_length_cap():
    out = _clean_title("word " * 40)
    assert len(out) <= 60


def test_clean_title_all_dates_falls_back():
    assert _clean_title("2026 July Monday") == "New Conversation"
