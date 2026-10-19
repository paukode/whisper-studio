/**
 * Markdown tables keep natural column widths, scroll in a frame when they
 * are wider than the message, and have resizable columns.
 *
 * The user's "Confirmed bugs" table rendered "Priority" one or two letters a
 * line and broke paths mid-name: `.markdown-content` sets the legacy
 * `word-break: break-word`, which lets an automatic-layout table size a
 * column as if a word could break between any two letters. jsdom has no
 * layout, so the stylesheet rules are pinned by reading markdown.css, the
 * way chatHistoryContainment.test.ts reads style.css.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import markdownCss from '../../../static/modules/markdown.css?raw';
import styleCss from '../../../static/style.css?raw';
import { MarkdownRenderer } from './MarkdownRenderer';
import { StreamingMarkdown } from './StreamingMarkdown';

const TABLE = [
  '| ID | Priority | Bug and verified behavior | Source |',
  '|---|---|---|---|',
  '| B1 | High | The chat bubble overflows on narrow windows | `public/style.css:106-110` |',
  '| B2 | High | The status badge never clears | `public/index.html:45` |',
].join('\n');

/** Every `selector { body }` rule in a stylesheet, comments dropped. */
function rules(css: string): { selector: string; body: string }[] {
  const plain = css.replace(/\/\*[\s\S]*?\*\//g, '');
  return [...plain.matchAll(/([^{}]+)\{([^{}]*)\}/g)].map((m) => ({
    selector: m[1].trim().replace(/\s+/g, ' '),
    body: m[2],
  }));
}

function rule(css: string, selector: string): string {
  const found = rules(css).find((r) => r.selector === selector);
  expect(found, selector).toBeDefined();
  return found!.body;
}

describe('markdown tables in the DOM', () => {
  it('puts a rendered table in a scrolling frame with a handle on each divider but the last', () => {
    const { container } = render(<MarkdownRenderer content={TABLE} />);
    const table = container.querySelector('.md-table-wrap > table');
    expect(table).not.toBeNull();
    const headers = table!.querySelectorAll('thead th');
    expect(headers).toHaveLength(4);
    expect(table!.querySelectorAll('.md-col-resize')).toHaveLength(headers.length - 1);
    expect(headers[3].querySelector('.md-col-resize')).toBeNull();
  });

  it('frames a streaming table on every token and adds the handles once the reply ends', () => {
    // Without the frame a wide table spilled out of the bubble and pushed the
    // chat panel sideways until the stream ended.
    const { container, rerender } = render(<StreamingMarkdown content={TABLE} isStreaming />);
    expect(container.querySelector('.md-table-wrap > table')).not.toBeNull();
    expect(container.querySelector('.md-col-resize')).toBeNull();

    rerender(<StreamingMarkdown content={`${TABLE}\n| B3 | Low | The retry button stays enabled | \`app.js:52\` |`} isStreaming />);
    expect(container.querySelectorAll('.md-table-wrap')).toHaveLength(1);
    expect(container.querySelectorAll('.md-table-wrap > table tbody tr')).toHaveLength(3);
    expect(container.querySelector('.md-col-resize')).toBeNull();

    rerender(<StreamingMarkdown content={TABLE} isStreaming={false} />);
    expect(container.querySelectorAll('.md-table-wrap')).toHaveLength(1);
    expect(container.querySelector('.md-table-wrap > table')).not.toBeNull();
    expect(container.querySelectorAll('.md-col-resize')).toHaveLength(3);
  });
});

describe('markdown table styles', () => {
  it('sizes columns from whole words: cells break words only at spaces', () => {
    const cells = rule(markdownCss, '.markdown-content th, .markdown-content td');
    expect(cells).toMatch(/word-break:\s*normal/);
    // break-word wraps a word that cannot fit without lowering the column's
    // minimum width, unlike anywhere (or the legacy word-break value).
    expect(cells).toMatch(/overflow-wrap:\s*break-word/);
  });

  it('never lets a table rule break words between any two letters', () => {
    const tableRules = rules(markdownCss).filter((r) => /\b(table|th|td)\b|md-table/.test(r.selector));
    expect(tableRules.length).toBeGreaterThan(0);
    for (const r of tableRules) {
      expect(r.body, r.selector).not.toMatch(/word-break:\s*break-(word|all)/);
    }
    // Prose outside tables keeps its wrapping.
    expect(rule(markdownCss, '.markdown-content')).toMatch(/word-break:\s*break-word/);
  });

  it('keeps a path or an identifier in a cell whole until the reader resizes the column', () => {
    const code = rule(markdownCss, '.markdown-content :is(th, td) code:not(pre code)');
    expect(code).toMatch(/white-space:\s*nowrap/);
    expect(code).toMatch(/word-break:\s*normal/);
    const resized = rule(markdownCss, '.markdown-content table.md-table-resized td code:not(pre code)');
    expect(resized).toMatch(/white-space:\s*normal/);
    expect(resized).toMatch(/overflow-wrap:\s*anywhere/);
  });

  it('scrolls a wide table inside its frame and fixes a resized one', () => {
    const wrap = rule(markdownCss, '.markdown-content .md-table-wrap');
    expect(wrap).toMatch(/overflow-x:\s*auto/);
    expect(wrap).toMatch(/max-width:\s*100%/);
    // The frame carries the outer margin, and sits flush as the first or
    // last block, as the bare table did.
    expect(rule(markdownCss, '.markdown-content table')).not.toMatch(/margin/);
    expect(
      rule(markdownCss, '.markdown-content > .md-table-wrap:first-child, .markdown-content blockquote > .md-table-wrap:first-child'),
    ).toMatch(/margin-top:\s*0/);
    expect(
      rule(markdownCss, '.markdown-content > .md-table-wrap:last-child, .markdown-content blockquote > .md-table-wrap:last-child'),
    ).toMatch(/margin-bottom:\s*0/);
    expect(rule(markdownCss, '.markdown-content table.md-table-resized')).toMatch(/table-layout:\s*fixed/);
    expect(rule(markdownCss, '.markdown-content table.md-table-resized td')).toMatch(/overflow-wrap:\s*anywhere/);
  });

  it('draws the divider in theme tokens and keeps it still for reduced motion', () => {
    expect(rule(markdownCss, '.markdown-content .md-col-resize')).toMatch(/cursor:\s*col-resize/);
    // Centered on the padding edge, where the collapsed border begins: an
    // offset of half a pixel drew the line beside the border, not on it.
    expect(rule(markdownCss, '.markdown-content .md-col-resize::after')).toMatch(/left:\s*50%;/);
    expect(rule(markdownCss, '.markdown-content .md-col-resize:hover::after')).toMatch(/var\(--border-strong\)/);
    const active = rule(
      markdownCss,
      '.markdown-content .md-col-resize.is-dragging::after, .markdown-content .md-col-resize:focus-visible::after',
    );
    expect(active).toMatch(/var\(--accent\)/);
    // The moving or focused line runs down the whole column, not the header only.
    expect(active).toMatch(/bottom:\s*calc\(-1 \* var\(--md-col-line, 0px\)\)/);
    const reduced = markdownCss.match(/@media \(prefers-reduced-motion: reduce\)\s*\{([\s\S]*?\})\s*\}/);
    expect(reduced?.[1]).toMatch(/\.md-col-resize::after\s*\{\s*transition:\s*none/);
  });

  it('styles chat tables in one place', () => {
    const chatTableRules = rules(styleCss).filter((r) =>
      /\.chat-msg\.assistant (table|th|td|tr)\b/.test(r.selector),
    );
    expect(chatTableRules.map((r) => r.selector)).toEqual([]);
    expect(rule(markdownCss, '.chat-msg.assistant .markdown-content table')).toMatch(/font-size:\s*var\(--fs-small\)/);
  });
});
