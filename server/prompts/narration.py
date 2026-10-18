"""How the assistant shows its work in a chat turn: a line before each group
of tool calls, what it found after a read, and a recap at the end.

Registered as the ``work_narration`` section of the cloud chat prompt
(server/prompts/__init__.py). The text is fixed, with no builder, so the
cached static block stays byte-stable for the whole session: brief mode and
plan mode are named in the text instead of switching the section off, which
would rewrite the cached prefix each time a mode flips. Headless runs,
scheduled tasks, agents and the on-device prompt build their own system
prompts and never read this one.

How the claim reader (server/goals/claims.py) reads the recap is pinned by
tests/test_gate_work_recap.py: under the table's change column each row is a
file the turn must have written (or, where the row's verb of removing takes
an object, written or removed), named by the write result's link; a file
only read is named in plain text, and line counts in any notation ("+18
-6", "18 added, 6 removed") claim nothing.
"""

WORK_NARRATION = (
    "\n\n## Showing your work\n"
    "Before a group of tool calls, say in one line what you are about to do and why. "
    "After reading or searching, say in one or two sentences what you found before you take "
    "the next step. When the work is done, close with a recap the reader can act on without "
    "the transcript: first one sentence with the outcome, then short bullets with a bold lead "
    "word for each change and why it was made, then a table of the files you wrote, edited or "
    "created in this turn (the name as the link the write result gave you, what changed, the "
    "added and removed line counts), then anything you left undone or could not verify. Name "
    "files you only read in plain text, not as links. Never claim a file was written unless "
    "you saw its write result this turn. In brief mode and in plan mode, skip the narration "
    "and the recap and follow those modes' own rules."
)
