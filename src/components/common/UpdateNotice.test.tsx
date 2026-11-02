import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const { getMock } = vi.hoisted(() => ({ getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock }));

import { UpdateNotice } from './UpdateNotice';
import { STORAGE_KEYS } from '@/utils/storageKeys';

const PAGE = 'https://github.com/paukode/whisper-studio/releases/tag/v2.16.0';

function check(over: Record<string, unknown> = {}) {
  return {
    current: '2.15.2',
    install: 'app',
    latest: { tag: 'v2.16.0', version: '2.16.0', url: PAGE, published_at: '2026-11-01T00:00:00Z' },
    update_available: true,
    update_commands: [],
    ...over,
  };
}

/** Let the mount fetch resolve and React commit. */
async function settle(): Promise<void> {
  await waitFor(() => expect(getMock).toHaveBeenCalledWith('/api/update-check'));
  await Promise.resolve();
}

describe('UpdateNotice', () => {
  beforeEach(() => {
    localStorage.clear();
    getMock.mockReset();
  });

  it('shows the newer version with an Update link to the release page', async () => {
    getMock.mockResolvedValue(check());
    render(<UpdateNotice />);
    expect(await screen.findByText(/A newer version of Whisper Studio is available \(v2\.16\.0\)/)).toBeInTheDocument();
    const update = screen.getByRole('link', { name: 'Update' });
    expect(update.getAttribute('href')).toBe(PAGE);
    // target=_blank is what the Mac app's web view hands to the default browser.
    expect(update.getAttribute('target')).toBe('_blank');
    expect(update.getAttribute('rel')).toContain('noopener');
    expect(screen.queryByText(/git pull/)).toBeNull();
  });

  it('stays hidden when the server reports no update', async () => {
    getMock.mockResolvedValue(check({ update_available: false }));
    render(<UpdateNotice />);
    await settle();
    expect(screen.queryByTestId('update-notice')).toBeNull();
  });

  it('stays hidden when the check fails', async () => {
    getMock.mockRejectedValue(new Error('offline'));
    render(<UpdateNotice />);
    await settle();
    expect(screen.queryByTestId('update-notice')).toBeNull();
  });

  it('Dismiss hides it for that version and remembers it', async () => {
    getMock.mockResolvedValue(check());
    const { unmount } = render(<UpdateNotice />);
    fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));
    expect(screen.queryByTestId('update-notice')).toBeNull();
    expect(localStorage.getItem(STORAGE_KEYS.UPDATE_DISMISSED)).toBe('2.16.0');
    unmount();

    render(<UpdateNotice />);
    await settle();
    expect(screen.queryByTestId('update-notice')).toBeNull();
  });

  it('a newer release than the dismissed one shows again', async () => {
    localStorage.setItem(STORAGE_KEYS.UPDATE_DISMISSED, '2.15.9');
    getMock.mockResolvedValue(check());
    render(<UpdateNotice />);
    expect(await screen.findByTestId('update-notice')).toBeInTheDocument();
  });

  it('a source install also shows the update commands', async () => {
    getMock.mockResolvedValue(check({ install: 'source', update_commands: ['git pull', 'bash setup.sh'] }));
    render(<UpdateNotice />);
    expect(await screen.findByText('git pull && bash setup.sh')).toBeInTheDocument();
  });

  it('still works when localStorage throws', async () => {
    const getItem = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const setItem = vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      getMock.mockResolvedValue(check());
      render(<UpdateNotice />);
      fireEvent.click(await screen.findByRole('button', { name: 'Dismiss' }));
      expect(screen.queryByTestId('update-notice')).toBeNull();
    } finally {
      getItem.mockRestore();
      setItem.mockRestore();
    }
  });
});
