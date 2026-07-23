---
name: summarize_transcript
description: Summarizes the live or recorded session transcript captured by this app in one of four fixed styles. Takes no text input; the server supplies the current transcript automatically and returns it with formatting instructions for the chosen style, from which the assistant writes the summary. Use when the user wants quick action items, key points, a short brief, or basic meeting notes of what was said in the current session. Do not use for notes or text the user pasted into chat, and prefer meeting_notes when the user wants a full polished write-up to share (it produces a richer structure). Not for attached files (use analyze_document). Returns a no-transcript message when nothing has been transcribed yet.
triggers: summarize, summary, action items, key points, takeaways, tldr, highlights, overview, recap, what was said
executor: summarize_transcript
input_schema:
  style:
    type: string
    required: true
    description: One of action_items (tasks and commitments with owners when mentioned), meeting_notes (attendees if identifiable, topics, decisions, actions), key_points (bulleted takeaways), or brief (2-3 sentences).
style_options: action_items, meeting_notes, key_points, brief
---

Runs server-side with no approval step. Returns the current session transcript together
with instructions for the requested style; the assistant then writes the summary.

Styles:

- **action_items**: every action item, task, and commitment, each with an owner when one
  is mentioned.
- **meeting_notes**: basic professional notes: attendees (only if identifiable from the
  transcript), topics discussed, decisions made, action items.
- **key_points**: the main takeaways as a bulleted list.
- **brief**: a 2-3 sentence summary.

Notes: an unknown style falls back to brief. When nothing has been transcribed yet the
tool returns "No transcript available to summarize." The transcript currently arrives
without speaker labels, so attendee lists depend on names spoken aloud. For a full
structured write-up (decisions table, blockers, next steps), the meeting_notes skill is
the better tool.
