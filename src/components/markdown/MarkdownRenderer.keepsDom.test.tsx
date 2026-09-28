/**
 * A re-render with the same content must keep the rendered DOM.
 *
 * React 19 assigns innerHTML whenever the dangerouslySetInnerHTML object is a
 * new one, so an inline `{ __html }` rebuilt every past chat message on each
 * streamed token: a long session spent seconds re-parsing and re-laying out
 * history, and a text selection or a scrolled code block reset under the
 * user. The nodes surviving a re-render is what proves that is gone.
 */
import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { MarkdownRenderer } from './MarkdownRenderer';

describe('MarkdownRenderer re-render', () => {
  it('keeps the same nodes when the parent re-renders with unchanged content', () => {
    const content = '## Title\n\nSome **bold** prose.\n\n```python\nprint(1)\n```';
    const { container, rerender } = render(<MarkdownRenderer content={content} stepFormat />);
    const heading = container.querySelector('h2');
    const pre = container.querySelector('pre');
    expect(heading).toBeTruthy();

    rerender(<MarkdownRenderer content={content} stepFormat />);

    expect(container.querySelector('h2')).toBe(heading);
    expect(container.querySelector('pre')).toBe(pre);
  });

  it('still replaces the nodes when the content changes', () => {
    const { container, rerender } = render(<MarkdownRenderer content="first" />);
    const before = container.querySelector('p');
    rerender(<MarkdownRenderer content="second" />);
    expect(container.querySelector('p')).not.toBe(before);
    expect(container.textContent).toContain('second');
  });
});
