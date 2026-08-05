import { beforeEach, describe, expect, it, vi } from 'vitest';

const { persistMock } = vi.hoisted(() => ({ persistMock: vi.fn() }));
vi.mock('@/api/notify', () => ({
  persistToastNotification: persistMock,
}));

import { useUIStore } from './uiStore';

describe('addToast → persistent notification log', () => {
  beforeEach(() => {
    persistMock.mockClear();
    useUIStore.getState().clearToasts();
  });

  it('records a durable notification for a plain toast', () => {
    useUIStore.getState().addToast({
      type: 'error',
      title: 'Download failed',
      message: 'Gemma 12B: connection reset',
      source: 'model-download',
    });
    expect(persistMock).toHaveBeenCalledTimes(1);
    expect(persistMock).toHaveBeenCalledWith({
      source: 'model-download',
      title: 'Download failed',
      message: 'Gemma 12B: connection reset',
      status: 'error',
    });
  });

  it('defaults source to undefined (server maps it to "app")', () => {
    useUIStore.getState().addToast({ type: 'info', message: 'heads up' });
    expect(persistMock).toHaveBeenCalledWith(
      expect.objectContaining({ source: undefined, status: 'info' }),
    );
  });

  it('skips persistence when persist:false (transient toasts)', () => {
    useUIStore.getState().addToast({ type: 'success', message: 'Saved', persist: false });
    useUIStore.getState().addToast({ type: 'info', message: 'Downloading 42%', persist: false });
    expect(persistMock).not.toHaveBeenCalled();
    // The ephemeral toasts still show.
    expect(useUIStore.getState().toasts).toHaveLength(2);
  });

  it('still persists when the toast merges into an existing keyed toast', () => {
    useUIStore.getState().addToast({ type: 'error', message: 'boom', key: 'k1' });
    useUIStore.getState().addToast({ type: 'error', message: 'boom', key: 'k1' });
    // Client-side the toast collapsed to one card; the server-side dedupe
    // window is what merges the log rows into a single count=2 entry.
    expect(useUIStore.getState().toasts).toHaveLength(1);
    expect(persistMock).toHaveBeenCalledTimes(2);
  });
});
