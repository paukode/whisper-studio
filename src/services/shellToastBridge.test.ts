import { beforeEach, describe, expect, it, vi } from 'vitest';

const { persistMock } = vi.hoisted(() => ({ persistMock: vi.fn() }));
vi.mock('@/api/notify', () => ({
  persistToastNotification: persistMock,
}));

import { useUIStore } from '@/stores/uiStore';
import { registerShellToastBridge } from './shellToastBridge';

/**
 * window.__whisperShellToast is the hook the macOS shell calls (via
 * evaluateJavaScript from macapp/shell/DownloadHandler.swift) to surface
 * download feedback in the app. Contract: registered on window, routes into
 * addToast, persists to the notification bell under source "export".
 */
describe('shell toast bridge', () => {
  beforeEach(() => {
    persistMock.mockClear();
    useUIStore.getState().clearToasts();
    delete window.__whisperShellToast;
  });

  it('registers window.__whisperShellToast', () => {
    expect(window.__whisperShellToast).toBeUndefined();
    registerShellToastBridge();
    expect(typeof window.__whisperShellToast).toBe('function');
  });

  it('shows a success toast and persists it to the bell with source "export"', () => {
    registerShellToastBridge();
    window.__whisperShellToast!('Saved to Downloads: conversation-abc.md', { type: 'success' });

    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0]).toMatchObject({
      type: 'success',
      message: 'Saved to Downloads: conversation-abc.md',
      source: 'export',
    });
    // Persisted (no persist:false) so the bell keeps it after auto-dismiss.
    expect(persistMock).toHaveBeenCalledTimes(1);
    expect(persistMock).toHaveBeenCalledWith(
      expect.objectContaining({
        source: 'export',
        message: 'Saved to Downloads: conversation-abc.md',
        status: 'success',
      }),
    );
  });

  it('routes error type through for failed downloads', () => {
    registerShellToastBridge();
    window.__whisperShellToast!('Download failed: disk full', { type: 'error' });
    expect(useUIStore.getState().toasts[0]).toMatchObject({
      type: 'error',
      message: 'Download failed: disk full',
    });
    expect(persistMock).toHaveBeenCalledWith(
      expect.objectContaining({ status: 'error' }),
    );
  });

  it('defaults to success when opts are missing and rejects junk types', () => {
    registerShellToastBridge();
    window.__whisperShellToast!('Saved to Downloads: a.txt');
    window.__whisperShellToast!('Saved to Downloads: b.txt', {
      type: 'explode' as unknown as 'success',
    });
    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(2);
    expect(toasts.every((t) => t.type === 'success')).toBe(true);
  });

  it('merges repeated identical messages into one toast card (5s dedupe key)', () => {
    registerShellToastBridge();
    window.__whisperShellToast!('Saved to Downloads: same.md', { type: 'success' });
    window.__whisperShellToast!('Saved to Downloads: same.md', { type: 'success' });
    const toasts = useUIStore.getState().toasts;
    expect(toasts).toHaveLength(1);
    expect(toasts[0].count).toBe(2);
  });

  it('ignores empty or non-string messages without throwing', () => {
    registerShellToastBridge();
    expect(() => {
      window.__whisperShellToast!('');
      (window.__whisperShellToast as (m: unknown) => void)(undefined);
    }).not.toThrow();
    expect(useUIStore.getState().toasts).toHaveLength(0);
    expect(persistMock).not.toHaveBeenCalled();
  });
});
