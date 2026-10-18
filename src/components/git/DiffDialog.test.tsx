import { describe, expect, it } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { DiffDialog, DiffGrid } from './DiffDialog';
import { collapseUnchanged, rowsFromHunks, rowsFromTexts } from '@/utils/diffRows';

/**
 * The diff grid alone: the Git panel's dialog body, the edit dialog's body,
 * and the approval card's preview. The rows come from the real diff code.
 */

/** Thirty numbered lines with line `changed` (1-based) replaced. */
const fileWith = (changed: number) =>
  Array.from({ length: 30 }, (_, i) => (i + 1 === changed ? `edited ${i + 1}` : `line ${i + 1}`)).join('\n');

/** One change in the middle: a collapsed gap before it and one after. */
const rowsFor = (changed: number) => collapseUnchanged(rowsFromTexts(fileWith(0), fileWith(changed)));

const gapButtons = (container: HTMLElement) => container.querySelectorAll('.git-diff-gap-btn');

/** The text of every code cell, without its sign column. */
const codeTexts = (container: HTMLElement) =>
  Array.from(container.querySelectorAll('.git-diff-code')).map((el) => el.textContent?.slice(1));

describe('DiffGrid', () => {
  it('expands a gap in place with the lines it hid', () => {
    const { container } = render(<DiffGrid rows={rowsFor(15)} layout="split" />);
    expect(gapButtons(container)).toHaveLength(2);
    expect(codeTexts(container)).not.toContain('line 1');

    fireEvent.click(gapButtons(container)[0]);

    expect(gapButtons(container)).toHaveLength(1);
    expect(codeTexts(container)).toContain('line 1');
  });

  it('closes every gap again when new rows arrive', () => {
    const { container, rerender } = render(<DiffGrid rows={rowsFor(15)} layout="unified" />);
    fireEvent.click(gapButtons(container)[0]);
    expect(gapButtons(container)).toHaveLength(1);

    // The next approval card reuses the same grid with other rows.
    rerender(<DiffGrid rows={rowsFor(16)} layout="unified" />);

    expect(gapButtons(container)).toHaveLength(2);
    expect(codeTexts(container)).not.toContain('line 1');
  });

  it('shows a gap from git hunks as a plain label: the client does not have its lines', () => {
    const rows = rowsFromHunks([
      {
        old_start: 1,
        new_start: 1,
        header: '',
        lines: [
          { type: 'del', old: 1, new: null, text: 'a' },
          { type: 'add', old: null, new: 1, text: 'A' },
        ],
      },
      {
        old_start: 20,
        new_start: 20,
        header: '',
        lines: [
          { type: 'del', old: 20, new: null, text: 'b' },
          { type: 'add', old: null, new: 20, text: 'B' },
        ],
      },
    ]);

    const { container } = render(<DiffGrid rows={rows} layout="split" />);

    expect(gapButtons(container)).toHaveLength(0);
    expect(container.querySelector('.git-diff-gap-label')?.textContent).toBe('... 18 unchanged lines');
  });
});

describe('DiffDialog focus', () => {
  it('takes the focus when it opens, keeps Tab inside, and gives the focus back on close', () => {
    const opener = document.createElement('button');
    opener.textContent = '+3 -1';
    document.body.appendChild(opener);
    opener.focus();
    try {
      const rows = rowsFromTexts('a\n', 'b\n');
      const view = render(
        <DiffDialog path="src/a.ts" status="modified" body={{ kind: 'rows', rows, empty: '' }} onClose={() => {}} />,
      );
      const close = view.getByRole('button', { name: 'Close' });
      expect(document.activeElement).toBe(close);
      // Tab from the last control comes round to the first, not the page.
      fireEvent.keyDown(document, { key: 'Tab' });
      const dialog = view.getByRole('dialog');
      expect(dialog.contains(document.activeElement)).toBe(true);
      view.unmount();
      expect(document.activeElement).toBe(opener);
    } finally {
      opener.remove();
    }
  });
});
