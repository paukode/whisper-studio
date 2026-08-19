import { describe, it, expect, vi } from 'vitest';
import { renderHook } from '@testing-library/react';
import type React from 'react';
import { useComposerDragDrop } from './useComposerDragDrop';

function pasteEvent(files: File[]) {
  const preventDefault = vi.fn();
  const e = { clipboardData: { files }, preventDefault } as unknown as React.ClipboardEvent;
  return { e, preventDefault };
}

describe('useComposerDragDrop paste', () => {
  it('leaves text-only pastes to the default insert', () => {
    const upload = vi.fn();
    const { result } = renderHook(() => useComposerDragDrop(upload));
    const { e, preventDefault } = pasteEvent([]);

    result.current.handlePaste(e);

    expect(preventDefault).not.toHaveBeenCalled();
    expect(upload).not.toHaveBeenCalled();
  });

  it('uploads pasted files as chips and suppresses the text flavor', () => {
    const upload = vi.fn();
    const { result } = renderHook(() => useComposerDragDrop(upload));
    const file = new File(['x'], 'report.pdf', { type: 'application/pdf' });
    const { e, preventDefault } = pasteEvent([file]);

    result.current.handlePaste(e);

    expect(preventDefault).toHaveBeenCalled();
    expect(upload).toHaveBeenCalledTimes(1);
    const [sent] = upload.mock.calls[0] as [File[]];
    expect(sent).toHaveLength(1);
    expect(sent[0].name).toBe('report.pdf');
  });

  it('stamps generically named clipboard images with a paste time', () => {
    const upload = vi.fn();
    const { result } = renderHook(() => useComposerDragDrop(upload));
    const screenshot = new File(['x'], 'image.png', { type: 'image/png' });

    result.current.handlePaste(pasteEvent([screenshot]).e);

    const [sent] = upload.mock.calls[0] as [File[]];
    expect(sent[0].name).toMatch(/^pasted-\d{4}-\d{2}-\d{2}-\d{2}-\d{2}-\d{2}\.png$/);
    expect(sent[0].type).toBe('image/png');
  });

  it('gives multiple generic images in one paste distinct names', () => {
    const upload = vi.fn();
    const { result } = renderHook(() => useComposerDragDrop(upload));
    const a = new File(['a'], 'image.png', { type: 'image/png' });
    const b = new File(['b'], '', { type: 'image/jpeg' });

    result.current.handlePaste(pasteEvent([a, b]).e);

    const [sent] = upload.mock.calls[0] as [File[]];
    expect(sent[0].name).not.toBe(sent[1].name);
    expect(sent[1].name).toMatch(/\.jpeg$/);
  });
});
