/** rawHtml={false}: tags in text another model wrote show as text, and the
 *  markdown around them still renders. */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { MarkdownRenderer } from './MarkdownRenderer';

describe('MarkdownRenderer rawHtml', () => {
  const source = '**bold** and <b>tag</b>\n\n<div class="x">block</div>\n';

  it('shows raw HTML as text when rawHtml is false', () => {
    const { container } = render(<MarkdownRenderer content={source} rawHtml={false} />);
    expect(container.querySelector('strong')?.textContent).toBe('bold');
    expect(container.querySelector('b')).toBeNull();
    expect(container.querySelector('div.x')).toBeNull();
    expect(container.textContent).toContain('<b>tag</b>');
    expect(container.textContent).toContain('<div class="x">block</div>');
  });

  it('keeps rendering raw HTML by default', () => {
    const { container } = render(<MarkdownRenderer content={source} />);
    expect(container.querySelector('b')?.textContent).toBe('tag');
  });
});
