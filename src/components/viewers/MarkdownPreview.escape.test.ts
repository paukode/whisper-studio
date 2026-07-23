/**
 * Unit test for the filename escaper used by MarkdownPreview's "Open in new
 * tab" / "Download as HTML" actions. The filename is interpolated into a
 * `<title>…</title>` in a document string, so a name containing `<`, `>` or
 * `&` (workspace files can be named anything) would otherwise corrupt the
 * markup or inject nodes. This asserts those characters are neutralised.
 */
import { describe, expect, it } from 'vitest';
import { escapeHtml } from './MarkdownPreview';

describe('escapeHtml', () => {
  it('escapes the HTML-significant characters', () => {
    expect(escapeHtml('<script>')).toBe('&lt;script&gt;');
    expect(escapeHtml('a & b')).toBe('a &amp; b');
    expect(escapeHtml('<img src=x onerror=alert(1)>')).toBe(
      '&lt;img src=x onerror=alert(1)&gt;',
    );
  });

  it('leaves a plain filename untouched', () => {
    expect(escapeHtml('README.md')).toBe('README.md');
    expect(escapeHtml('notes-2026.md')).toBe('notes-2026.md');
  });

  it('escapes every occurrence, not just the first', () => {
    expect(escapeHtml('a<b<c')).toBe('a&lt;b&lt;c');
    expect(escapeHtml('&&&')).toBe('&amp;&amp;&amp;');
  });
});
