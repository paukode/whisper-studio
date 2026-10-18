/**
 * Plain path mentions in markdown become links that open the file in the
 * editor.
 *
 * A pre-pass on the markdown SOURCE, run before marked by MarkdownRenderer
 * and StreamingMarkdown. A workspace-relative path written as plain text,
 * such as src/hooks/chatStream/sseStream.ts:282, becomes
 * [src/hooks/chatStream/sseStream.ts:282](#wsfile=<absolute path>&L=282-282),
 * the citation link wsFileLinks.ts opens in the side panel at those lines.
 * With no workspace connected the text is left alone.
 *
 * A mention is a token with at least one slash, path characters only (the
 * letters and digits of any script, so docs/über/a.md is one path) and a
 * file extension of 1 to 8 letters or digits (with one letter at least, so a
 * version or a fraction is not a file), optionally followed by :line or
 * :start-end. It does not start inside a word, a longer path or an address,
 * and its first folder has no dot in it but a leading one (.github), so
 * example.com/a.html stays text.
 *
 * Never touched: fenced code blocks, inline code spans, raw HTML code (pre,
 * code, kbd and samp elements), HTML blocks (a line that opens with a block
 * tag such as <details>, <summary> or <table>, to the next blank line:
 * marked reads no markdown there, so a link would show as its source),
 * markdown links and images (their labels too), reference definitions,
 * autolinks, HTML tags and web addresses. While a reply streams, a code
 * span whose closing backticks have not arrived yet runs to the end of the
 * text (the `streaming` option). A write result's link (#wsfile=...&open=os)
 * is a link already, so it keeps opening with the OS app. An indented code
 * block (four spaces, no fence) cannot be told from a list item's text
 * without parsing the list, so it reads as prose.
 *
 * One regex pass, because the streaming view re-parses on every token: the
 * alternatives ahead of the path match what must stay as it is, and since
 * the leftmost match wins, a path inside one of them is never seen. Each
 * scan that may not find its end stops at a bound (a newline, the next "<",
 * 32 characters of a scheme) or reads its run once (a lookahead the match
 * then takes whole), so no input makes the pass quadratic. A
 * backtick is written \x60: in a raw string an escaped backtick keeps its
 * backslash, which the u flag (for \p{L}) rejects.
 */

// A word character in any script, for the path and the ones around it.
const W = String.raw`\p{L}\p{M}\p{N}_`;

const SKIP = [
  // A fenced code block, backticks or tildes, to its closing fence or to the
  // end of the text while it is still streaming. Indented or quoted fences
  // (inside a list item or a blockquote) count too.
  String.raw`^[ \t>]*(?<bt>\x60{3,})[^\x60\n]*(?:\n[\s\S]*?)?(?:\n[ \t>]*\k<bt>\x60*[ \t]*(?=\n|$)|(?![\s\S]))`,
  String.raw`^[ \t>]*(?<tl>~{3,})[^\n]*(?:\n[\s\S]*?)?(?:\n[ \t>]*\k<tl>~*[ \t]*(?=\n|$)|(?![\s\S]))`,
  // An inline code span: a run of backticks to the next run of the same
  // length, within one paragraph.
  String.raw`(?<!\x60)(?<cs>\x60+)(?!\x60)(?:[^\n]|\n(?![ \t]*\n))*?(?<!\x60)\k<cs>(?!\x60)`,
  // A markdown link or image, inline or by reference.
  String.raw`!?\[(?:[^\[\]\n]|\[[^\[\]\n]*\])*\](?:\((?:[^()\n]|\([^()\n]*\))*\)|\[[^\]\n]*\])`,
  // A reference definition: [id]: target
  String.raw`^[ \t]{0,3}\[[^\]\n]+\]:[^\n]*`,
  // An HTML block (CommonMark's kind 6): a line that opens with a block
  // tag, to the next blank line.
  String.raw`^[ \t]{0,3}<\/?(?:address|article|aside|blockquote|details|dialog|div|dl|figure|footer|form|h[1-6]|header|li|nav|ol|p|section|summary|table|tbody|td|tfoot|th|thead|tr|ul)(?![${W}-])[^\n]*(?:\n(?![ \t]*$)[^\n]*)*`,
  // Raw HTML code, to its closing tag or, like a fence, to the end of the
  // text while it is still streaming: a link written inside would show as
  // its markdown.
  String.raw`<(?<raw>pre|code|kbd|samp)\b[^<>]*>[\s\S]*?(?:<\/\k<raw>>|(?![\s\S]))`,
  // An HTML anchor with its text (read up to the next tag only, so an anchor
  // never closed is not scanned to the end of the text), then any other tag,
  // comment or autolink.
  String.raw`<[aA](?:\s[^<>]*)?>[^<]*<\/[aA]>`,
  String.raw`<[A-Za-z\/!?][^<>\n]*>`,
  // A web address, whatever its scheme.
  String.raw`\b[A-Za-z][A-Za-z0-9+.-]{0,31}:\/\/[^\s<>"'\x60]*`,
];

const PATH =
  // Not inside a word, a longer path, an address or an unclosed code span.
  String.raw`(?<![${W}./\\@+~:%=&#$\x60-])` +
  String.raw`(?<path>(?:\.\/)?\.?[${W}][${W}@+-]*(?:\/\.?[${W}@+-][${W}.@+-]*)*?\/\.?[${W}@+-][${W}.@+-]*?` +
  String.raw`\.(?=[A-Za-z0-9]{0,7}[A-Za-z])[A-Za-z0-9]{1,8})(?![${W}@+\/-]|\.[${W}])` +
  String.raw`(?::(?<start>\d+)(?:-(?<end>\d+))?)?`;

// While a reply streams: a code span whose closing run has not arrived yet,
// from its backticks to the end of the text within one paragraph. After the
// closed span above, so a span that is closed is read as one. Its run is
// read once (the lookahead), so a stray backtick earlier costs one scan.
const OPEN_CODE_SPAN =
  String.raw`(?<!\x60)\x60+(?!\x60)(?=(?<open>(?:[^\n]|\n(?![ \t]*\n))*))\k<open>(?![\s\S])`;

const TOKEN_RE = new RegExp([...SKIP, PATH].join('|'), 'gmu');
const STREAMING_TOKEN_RE = new RegExp([...SKIP, OPEN_CODE_SPAN, PATH].join('|'), 'gmu');

/** The path url-quoted as Python's quote() does it (server/index/citations.py):
 *  every character but letters, digits, _.-~ and / is escaped, so a "&" or
 *  ":" in a name never reads as the fragment's own separator. */
function quotePath(path: string): string {
  return encodeURIComponent(path)
    .replace(/%2F/g, '/')
    .replace(/[!'()*]/g, (ch) => `%${ch.charCodeAt(0).toString(16).toUpperCase()}`);
}

interface MentionGroups {
  path?: string;
  start?: string;
  end?: string;
}

/** Turn the plain path mentions in ``md`` into editor links against the
 *  workspace root ``wsRoot``; ``md`` unchanged when no workspace is
 *  connected or it names no path. ``streaming``: ``md`` is a reply still
 *  arriving, whose last code span may not be closed yet. */
export function linkifyPaths(md: string, wsRoot: string, { streaming = false } = {}): string {
  const root = (wsRoot || '').replace(/\/+$/, '');
  if (!md || !root || !md.includes('/')) return md;
  return md.replace(streaming ? STREAMING_TOKEN_RE : TOKEN_RE, (...args: unknown[]) => {
    const match = args[0] as string;
    const groups = args[args.length - 1] as MentionGroups;
    if (!groups.path) return match;
    const relative = groups.path.replace(/^\.\//, '');
    let href = `#wsfile=${quotePath(`${root}/${relative}`)}`;
    const start = groups.start ? parseInt(groups.start, 10) : 0;
    if (start >= 1) {
      const end = groups.end ? parseInt(groups.end, 10) : start;
      href += `&L=${start}-${end}`;
    }
    // An underscore in a name ("__init__.py") would read as emphasis.
    const label = match.replace(/_/g, '\\_');
    return `[${label}](${href})`;
  });
}
