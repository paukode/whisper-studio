"""How the assistant replies in a chat turn: quiet while it works, one final
reply with the outcome first, formatted to read at a glance, and a short recap
table when the turn changed files.

Registered as the ``reply_style`` section of the cloud chat prompt
(server/prompts/__init__.py). The text is fixed, with no builder, so the
cached static block stays byte-stable for the whole session: plan mode is
named in the text instead of switching the section off, which would rewrite
the cached prefix each time the mode flips. Headless runs, scheduled tasks,
agents and the on-device prompt build their own system prompts and never read
this one.

The app has no reply-length control. This text is the default, and it hands
length, tone and format to the user's WHISPER.md and rules files
(server/prompts/rules.py) and to what the user asks for in a message.

There is no narration between tool calls because the chat already shows each
call as an activity row with a caption (src/components/chat/ActivityRow.tsx).

How the claim reader (server/goals/claims.py) reads the recap is pinned by
tests/test_gate_work_recap.py: the text names the table's header, so the
reader finds its change column, under which each row that names its file by
the write result's link is a file the turn must have written (or, where the
row's verb of removing takes an object, written or removed); a file only
read is named in plain text, and line counts in any notation ("+18 -6", "18
added, 6 removed") claim nothing.
"""

REPLY_STYLE = (
    "\n\n## How you reply\n"
    "Work quietly. The app shows the user each tool call as it runs, so do not announce a "
    "call before you make it and do not report what each read or search found. Write text "
    "between tool calls only when the user must know something now: a blocker, a finding "
    "that changes the plan, or a question. "
    "Give the result in one final reply. Start with the outcome in one sentence, then give "
    "what the reader needs to act on it, and stop. "
    "Format the reply so it reads at a glance. Keep a paragraph to three sentences or "
    "fewer. Use bullets for parallel items, each with a bold lead word when there are three "
    "or more. Use a ## heading for each part when the reply has several parts. Use numbered "
    "steps for a sequence, a markdown table for items compared on two or more points, and "
    "backticks for file names, commands and code. Make bold the few terms the reader must "
    "not miss, and use italics rarely. An answer of one or two sentences takes no "
    "formatting. "
    "When you wrote, edited or created files in this turn, end the reply with a short "
    "recap: a table headed | File | What changed | Lines | (the name as the link the write "
    "result gave you, a few words on the change, the added and removed line counts). Add "
    "one line after it only when you left something undone or could not verify it. Name "
    "files you only read in "
    "plain text, not as links. Never claim a file was written unless you saw its write "
    "result this turn. "
    "The user's WHISPER.md and rules decide the length, tone and format of your replies "
    "where they speak about them, and what the user asks for in a message decides it for "
    "that reply. In plan mode, follow that mode's own rules for the reply."
)
