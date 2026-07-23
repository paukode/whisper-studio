---
name: meeting_notes
description: Turns meeting text into a polished, structured write-up with overview, decisions, an owner/action/deadline table, discussion points, blockers, and next steps. Use when the user pastes rough notes, bullets, or transcript text and wants minutes or a clean summary to share, or asks for a full write-up of the current recorded meeting (omit notes and the live transcript is used automatically). Skips empty sections, keeps technical detail, never invents content, and flags gaps at the end. For a quick action-items list, key points, or a two-sentence brief of the live session, use summarize_transcript instead; for a late-joiner catch-up with suggested questions, use catch_up.
triggers: meeting notes, meeting summary, summarise meeting, summarize meeting, write up meeting, meeting minutes, minutes, call notes, what we discussed, action items from meeting
input_schema:
  notes:
    type: string
    required: true
    description: Full raw meeting notes or transcript text, verbatim and untrimmed. If omitted or too short while a live recording exists, the current transcript is injected automatically.
---

# Meeting Notes Summariser

Turn the provided meeting text into a summary that actually gets read: clear, direct,
and written like a sharp colleague who sat in the room. Not a press release, not a
status report.

## What to produce

Read the notes properly. Understand what actually happened, what matters, and what
people need to do next. Then write it up with this structure. It is a sensible default,
not a rigid contract: skip any section with nothing in it, and when you skip a section,
omit its heading entirely. Omit the "Who was there" line if attendees cannot be
identified, and drop the date parentheses from the title entirely when no date is
mentioned (never emit empty parentheses). A summary with three real sections beats a
padded one with eight.

```
## [Project / Topic] ([Date if mentioned])
**Who was there:** [names or roles]

### What we covered
[2-3 sentences. What was this meeting actually about? What was the goal?
Write it like you're telling a colleague who missed it, not like a formal report.]

### Decisions made
- [State what was agreed, plainly. Not "it was decided that...", just say what was decided.]

### Actions
| Who | What | By when |
|-----|------|---------|
| [name] | [specific thing they're doing] | [date or TBC] |

### What came up
[The main discussion threads, grouped by topic if there are clearly separate ones.
One or two lines per point is usually enough.]

### Blockers or issues
- [What's in the way, and why it matters]

### Next up
- [What's happening next, or what people are waiting on]
```

## How to write it

- **Cut the filler.** Don't open sentences with "It was noted that", "The team
  discussed", or "Moving forward". Just say the thing.
- **Keep technical terms when they matter.** If the notes mention a model name, a
  system, a tool, a query: keep it. The people reading this need that detail.
- **Be specific on actions.** "Zara to check why the SAP extract is dropping rows for
  plant 1000" is useful. "Zara to investigate data issue" is not.
- **Match the length to the meeting.** A 15-minute check-in gets a short summary. A
  2-hour deep-dive with decisions and debates gets the space it deserves.
- **Don't invent.** If something wasn't in the notes, leave it out. No guessed owners,
  no guessed deadlines (use TBC), no gap-filling. Keep relative dates as given
  ("by Friday"), don't convert them to calendar dates.
- **No em dashes, no emoji, and no horizontal-rule separator lines** (`---`) anywhere
  in the output, matching the app-wide style rules. Headings do the separating; set
  the closing flag note off with a blank line and italics instead of a rule.

## If the input is a transcript

Raw speech-to-text needs extra care:

- Derive attendees from speaker labels when present; otherwise leave "Who was there" out.
- Drop greetings, small talk, filler words, and false starts. Compress repeated
  back-and-forth into the point it resolved to.
- Transcription mangles names, numbers, and dates. Treat suspicious ones cautiously
  and list them in the closing flag note instead of presenting them as fact.
- Summary length follows the substance, not the transcript length.

## When notes are messy or incomplete

Do your best with what's there, then add a short note at the bottom flagging anything
unclear or missing, so the user can patch it before sending.

## Before you deliver

1. Every name, date, number, decision, and action traces back to the input.
2. Every action row has a who, a what, and a date or TBC.
3. Skipped sections are fully gone, headings included.
4. No filler openers, no em dashes, no emoji, no `---` separator lines.
5. Unclear or uncertain items are flagged in the closing note.
