---
name: analyze_document
description: Retrieves the content of a file the user attached to the chat so the assistant can answer a question from it. Returns the whole document capped at 150,000 characters, or one section in full when section is set to a number from the attachment's outline. Use when an attached document's inline preview was truncated (the message shows an outline plus a first slice) or a specific section is needed in full. Do not use for small attachments already fully visible in the conversation, for images (already visible, analyze directly), for workspace files (use ws_read_file), or for the live session transcript (use summarize_transcript). Results over 50KB are truncated by the tool-result budget to a short head plus a cache-file reference, so prefer section fetches for large documents. An unmatched filename returns the list of available attachments.
triggers: analyze document, attached file, analyze pdf, read attachment, document section, compare documents, extract from file, what does the file say
executor: analyze_document
input_schema:
  filename:
    type: string
    required: true
    description: Exact filename of the attachment as shown in the conversation; matching is exact and case-sensitive, and a mismatch returns the available filenames.
  question:
    type: string
    required: true
    description: The analysis question or task. It is echoed back with the content and does not affect what is retrieved.
  section:
    type: string
    required: false
    description: Section number from the attachment outline, as a string like "3". Returns that section in full. Only works for documents with headings.
---

Runs server-side with no approval step. Looks up the attachment by exact filename and
returns its stored text plus the question, so the assistant can answer from it.

Behavior worth knowing:

- Whole-file fetches are capped at 150,000 characters. On top of that, any tool result
  over 50KB is truncated by the chat budget layer to a short head plus a
  `.whisper_cache` file reference, so large documents should be fetched section by
  section using `section` numbers from the outline that was shown when the file was
  attached.
- Images are not fetchable this way: they are already visible in the conversation and
  the tool answers with a redirect note.
- An unknown filename returns the list of available attachments instead of failing.
