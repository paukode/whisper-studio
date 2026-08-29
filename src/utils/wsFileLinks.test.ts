import { describe, it, expect } from 'vitest';
import { parseDocsPageHref, parseWsFileHref } from './wsFileLinks';
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

  it('parses a created-file link (server.index.citations.created_file_link)', () => {
    expect(parseWsFileHref('#wsfile=/w/report.md&open=os')).toEqual({
      path: '/w/report.md',
      openMode: 'os',
    });
  });

  it('ignores &open= for anything other than the exact os marker', () => {
    expect(parseWsFileHref('#wsfile=/w/a.md&open=dock')).toEqual({ path: '/w/a.md' });
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

  it('keeps a created-file link with the &open=os param and parses back', () => {
    const html = renderMarkdownSafe('[report.md](#wsfile=/w/report.md&open=os)');
    const div = document.createElement('div');
    div.innerHTML = html;
    const href = div.querySelector('a')?.getAttribute('href') ?? '';
    expect(parseWsFileHref(href)).toEqual({ path: '/w/report.md', openMode: 'os' });
  });
});

describe('parseDocsPageHref', () => {
  it('parses a page with a heading anchor', () => {
    expect(parseDocsPageHref('#docspage=tut-cron.html&h=create-a-job')).toEqual({
      page: 'tut-cron.html',
      anchor: 'create-a-job',
    });
  });

  it('parses a bare page and url-decodes it', () => {
    expect(parseDocsPageHref('#docspage=ref-slash-commands.html')).toEqual({
      page: 'ref-slash-commands.html',
    });
    expect(parseDocsPageHref('#docspage=tut-cron.html')).toEqual({ page: 'tut-cron.html' });
  });

  it('rejects traversal, nested paths, and non-html targets', () => {
    expect(parseDocsPageHref('#docspage=..%2F..%2Fetc%2Fpasswd')).toBeNull();
    expect(parseDocsPageHref('#docspage=assets%2Fsite.js')).toBeNull();
    expect(parseDocsPageHref('#docspage=%2Fabs.html')).toBeNull();
  });

  it('returns null for non-docs hrefs', () => {
    expect(parseDocsPageHref('#wsfile=a.md')).toBeNull();
    expect(parseDocsPageHref('https://example.com')).toBeNull();
  });
});
