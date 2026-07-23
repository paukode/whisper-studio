/**
 * The readiness gate: the iframe must not mount until the target answers, so a
 * slow-booting dev server (FastAPI/Django/…) never shows Chromium's cached
 * ERR_CONNECTION_REFUSED page. The probe keeps retrying, so a late boot
 * self-heals into the live iframe (the auto-retry).
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, cleanup } from '@testing-library/react';

// Keep the API layer inert — we only care about the render gate here.
vi.mock('@/api/preview', () => ({
  startPreviewSession: vi.fn(),
  stopPreviewSession: vi.fn(),
  createScreencastSocket: vi.fn(),
}));

import { LiveBrowserPanel } from './LiveBrowserPanel';
import { useDockStore } from '@/stores/dockStore';

const TARGET = 'http://localhost:65500';

function seedRunningSession() {
  useDockStore.setState({
    liveSession: { name: 'test', url: TARGET, port: 65500 },
    liveNavUrl: null,
    panels: [{ id: 'live', kind: 'live', title: 'Live · test' }],
  });
}

const iframe = () => document.querySelector('iframe');

beforeEach(() => {
  seedRunningSession();
});
afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('LiveBrowserPanel — readiness gate', () => {
  it('shows a waiting state and no iframe while the server refuses connections', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));

    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} />);

    expect(await screen.findByText(/Waiting for the dev server/i)).toBeInTheDocument();
    expect(iframe()).toBeNull();
  });

  it('mounts the iframe once the server answers', async () => {
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({}));

    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} />);

    await waitFor(() => expect(iframe()).not.toBeNull(), { timeout: 4000 });
    expect(iframe()?.getAttribute('src')).toBe(TARGET);
  });

  it('auto-retries: waiting first, then flips to the iframe when the server comes up', async () => {
    let up = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(() => (up ? Promise.resolve({}) : Promise.reject(new TypeError('refused')))),
    );

    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} />);

    // Server still booting → gated.
    expect(await screen.findByText(/Waiting for the dev server/i)).toBeInTheDocument();
    expect(iframe()).toBeNull();

    // Server finishes booting; the probe loop should catch it and mount.
    up = true;
    await waitFor(() => expect(iframe()).not.toBeNull(), { timeout: 4000 });
  });
});
