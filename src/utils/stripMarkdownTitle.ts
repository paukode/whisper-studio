/**
 * Strip common Markdown tokens from a one-line string so auto-derived session
 * titles read as clean text in the sidebar.
 *
 * Titles are derived from the first message, so when a message opens with
 * Markdown the raw tokens leak through literally: "# Your choice matters",
 * "**I cannot" (a cut-off bold marker), "> Note to self". This is a
 * display-only cleanup — the stored title is untouched and rename still edits
 * the raw value. Kept intentionally conservative: it removes marker
 * punctuation but never rewrites the words, and falls back to the trimmed
 * input if stripping would leave nothing.
 */
export function stripMarkdownTitle(raw: string | null | undefined): string {
  if (!raw) return raw ?? '';
  let s = raw.trim();

  // Leading block markers, possibly stacked ("> # "): ATX headings, blockquote
  // arrows, unordered bullets, and ordered-list numbers.
  s = s.replace(/^\s*(?:#{1,6}\s+|>\s*|[-*+]\s+|\d+[.)]\s+)+/, '');

  // Images ![alt](url) -> alt, then links [text](url) -> text.
  s = s.replace(/!\[([^\]]*)\]\([^)]*\)/g, '$1');
  s = s.replace(/\[([^\]]*)\]\([^)]*\)/g, '$1');

  // Inline emphasis / code / strike markers, including dangling ones left by a
  // truncated first message ("**I cannot"). Underscores are left alone so
  // snake_case identifiers in a title survive.
  s = s.replace(/\*{1,3}|`+|~~/g, '');

  // Collapse the whitespace the removals may have opened up.
  s = s.replace(/\s+/g, ' ').trim();

  return s || raw.trim();
}
