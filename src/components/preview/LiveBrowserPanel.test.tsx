/**
 * The readiness gate: the iframe must not mount until the target answers, so a
 * slow-booting dev server (FastAPI/Django/…) never shows Chromium's cached
 * ERR_CONNECTION_REFUSED page. The probe keeps retrying, so a late boot
 * self-heals into the live iframe (the auto-retry).
 */
import { describe, expect, it, beforeEach, afterEach, vi } from 'vitest';
import { render, screen, waitFor, cleanup, fireEvent } from '@testing-library/react';

// Keep the API layer inert — we only care about the render gate here.
vi.mock('@/api/preview', () => ({
  startPreviewSession: vi.fn(),
  stopPreviewSession: vi.fn(),
  createScreencastSocket: vi.fn(),
}));

import { LiveBrowserPanel } from './LiveBrowserPanel';
import { useDockStore } from '@/stores/dockStore';
import { useSessionStore } from '@/stores/sessionStore';
import { startPreviewSession, stopPreviewSession } from '@/api/preview';

const TARGET = 'http://localhost:65500';
const CHAT = 'chat-a';

function seedRunningSession() {
  useSessionStore.setState({ currentSessionId: CHAT });
  useDockStore.setState({
    liveSession: { name: 'test', url: TARGET, port: 65500, owner: CHAT },
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

// Field report: stopping the debugging chat's work stopped the app another
// chat was building. The pane's Stop must act only on the server this chat
// owns, and only while the pane is showing that server.
describe('LiveBrowserPanel: Stop and Restart ownership', () => {
  const stopBtn = () => screen.queryByRole('button', { name: 'Stop' });

  beforeEach(() => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('refused')));
    (stopPreviewSession as unknown as ReturnType<typeof vi.fn>).mockReset();
    (startPreviewSession as unknown as ReturnType<typeof vi.fn>).mockReset();
  });

  it("stops this chat's own server, naming this chat to the backend", () => {
    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} owner={CHAT} />);
    fireEvent.click(stopBtn()!);
    expect(stopPreviewSession).toHaveBeenCalledWith('test', CHAT);
  });

  it('offers no Stop while a routed URL points at a different server', () => {
    useDockStore.setState({ liveNavUrl: 'http://localhost:5174/debug' });
    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} owner={CHAT} />);
    expect(screen.getByLabelText('Preview URL')).toHaveValue('http://localhost:5174/debug');
    expect(stopBtn()).toBeNull();
  });

  it('keeps Stop for a routed URL on the same server (loopback alias, other path)', () => {
    useDockStore.setState({ liveNavUrl: 'http://127.0.0.1:65500/settings' });
    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} owner={CHAT} />);
    expect(stopBtn()).not.toBeNull();
  });

  it("offers no Stop for a preview another chat owns, even before the watcher catches up", () => {
    useSessionStore.setState({ currentSessionId: 'chat-b' });
    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} owner={CHAT} />);
    expect(stopBtn()).toBeNull();
  });

  it("a stopped pane restarts only this chat's server", () => {
    useDockStore.setState({ liveSession: null });
    const { unmount } = render(<LiveBrowserPanel name="test" url={TARGET} port={65500} owner={CHAT} />);
    fireEvent.click(screen.getByRole('button', { name: 'Restart' }));
    expect(startPreviewSession).toHaveBeenCalledWith('test', CHAT);
    unmount();

    // Another chat on screen: the leftover pane says so and offers no Restart.
    useSessionStore.setState({ currentSessionId: 'chat-b' });
    render(<LiveBrowserPanel name="test" url={TARGET} port={65500} owner={CHAT} />);
    expect(screen.getByText('No preview is running in this chat')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: 'Restart' })).toBeNull();
  });
});
