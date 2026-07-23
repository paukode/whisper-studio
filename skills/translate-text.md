---
name: translate_text
description: Translates text into a target language, replying with only the translation and preserving the original formatting, line breaks, and structure. Use when the user asks to translate a phrase, passage, document excerpt, or the live transcript. Pass the exact text verbatim in content, even when it is short; to translate the current session transcript, omit content and follow the skill instructions. Not for detecting languages or transcribing audio.
triggers: translate, translation, translate to, translate this, in another language, to english, to spanish, to kurdish, localize
input_schema:
  content:
    type: string
    required: false
    description: Exact text to translate, verbatim and complete. Omit to translate the live session transcript instead.
  target_language:
    type: string
    required: true
    description: Target language name, with dialect or script when relevant, e.g. Spanish, Kurdish (Sorani), Chinese (Simplified).
---

Translate the provided content into the target language.

- If `content` is empty, translate the "[Transcript so far]" block earlier in this
  conversation. If there is no transcript either, ask what to translate.
- Output ONLY the translation: no preamble, no commentary, no romanization unless asked.
- Preserve the original formatting and structure: line breaks, lists, headings,
  tables, and code blocks stay where they were. Do not translate code, identifiers,
  URLs, or proper nouns unless the user asks.
- If the target language has major dialect or script variants and the user did not
  specify one (e.g. Kurdish Sorani vs Kurmanji, Simplified vs Traditional Chinese),
  pick the most common variant and note the choice in one short line after the
  translation. When the user already named the variant, output the translation only,
  with no note.
