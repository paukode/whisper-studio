/**
 * Copy Rich Text fills the copy event that execCommand('copy') fires: the
 * sanitized HTML for apps that read formatting, the markdown as plain text.
 * A refused copy reports false, and the listener never outlives the call, so
 * the user's own Cmd+C afterwards copies what they selected.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { copyRichText } from './messageActions';
import { copyEvent, stubExecCommand } from '@/test/clipboardCopy';

afterEach(() => {
  Reflect.deleteProperty(document, 'execCommand');
  vi.restoreAllMocks();
});

describe('copyRichText', () => {
  it('puts the sanitized HTML and the markdown on the clipboard in one copy event', () => {
    const copy = stubExecCommand(true);
    const markdown = '**Bold** and <img src="x" onerror="alert(1)">';
    expect(copyRichText(markdown)).toBe(true);
    expect(copy.exec).toHaveBeenCalledWith('copy');
    expect(copy.data.get('text/plain')).toBe(markdown);
    expect(copy.data.get('text/html')).toContain('<strong>Bold</strong>');
    expect(copy.data.get('text/html')).not.toContain('onerror');
    // The browser's own copy of the page selection is replaced, not added to.
    expect(copy.event.defaultPrevented).toBe(true);
  });

  it('copies unset content as empty text instead of throwing', () => {
    const copy = stubExecCommand(true);
    expect(copyRichText(undefined)).toBe(true);
    expect(copy.data.get('text/plain')).toBe('');
  });

  it('reports a refused copy, so the caller can say so', () => {
    const copy = stubExecCommand(false);
    expect(copyRichText('**Bold**')).toBe(false);
    expect(copy.data.size).toBe(0);
  });

  it('leaves a later copy alone', () => {
    stubExecCommand(true);
    copyRichText('**Bold**');
    const later = copyEvent();
    document.dispatchEvent(later.event);
    expect(later.data.size).toBe(0);
    expect(later.event.defaultPrevented).toBe(false);
  });
});
