import { describe, it, expect } from 'vitest';
import { parseWsFileHref } from './wsFileLinks';
import { renderMarkdownSafe } from './sanitizeHtml';

describe('parseWsFileHref', () => {
  it('parses a legacy link with no line param', () => {
    expect(parseWsFileHref('#wsfile=a%20b.md')).toEqual({ path: 'a b.md' });
  });

  it('parses a line range', () => {
    expect(parseWsFileHref('#wsfile=/w/a.md&L=3-9')).toEqual({
      path: '/w/a.md',
      startLine: 3,
      endLine: 9,
    });
  });

  it('parses a single line as a one-line range', () => {
    expect(parseWsFileHref('#wsfile=/w/a.md&L=7')).toEqual({
      path: '/w/a.md',
      startLine: 7,
      endLine: 7,
    });
  });

  it('swaps a reversed range and ignores malformed params', () => {
    expect(parseWsFileHref('#wsfile=/w/a.md&L=40-12')).toMatchObject({ startLine: 12, endLine: 40 });
    expect(parseWsFileHref('#wsfile=/w/a.md&L=x')).toEqual({ path: '/w/a.md' });
    expect(parseWsFileHref('#wsfile=/w/a.md&L=0')).toEqual({ path: '/w/a.md' });
  });

  it('splits before decoding so encoded & and : in the path survive', () => {
    // path "a & b:1.md" quoted -> a%20%26%20b%3A1.md ; first raw & is the boundary
    expect(parseWsFileHref('#wsfile=a%20%26%20b%3A1.md&L=2-4')).toEqual({
      path: 'a & b:1.md',
      startLine: 2,
      endLine: 4,
    });
  });

  it('returns null for non-wsfile hrefs', () => {
    expect(parseWsFileHref('https://example.com')).toBeNull();
    expect(parseWsFileHref('#other=1')).toBeNull();
  });
});

describe('citation link survives marked + DOMPurify', () => {
  it('keeps the #wsfile href with the &L param and parses back', () => {
    const html = renderMarkdownSafe('[a.md:3-9](#wsfile=/w/a.md&L=3-9)');
    const div = document.createElement('div');
    div.innerHTML = html;
    const href = div.querySelector('a')?.getAttribute('href') ?? '';
    expect(parseWsFileHref(href)).toEqual({ path: '/w/a.md', startLine: 3, endLine: 9 });
  });
});
