import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { ApiError } from '@/types/api';

// The gate talks to /api/models/* through the shared api client; stub it so
// every request is programmable (same approach as ModelsPanel.test.tsx).
const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  put: vi.fn(),
}));
vi.mock('@/api/client', () => api);

import { ensureRecordingModels, requiredRecordingModelKeys } from './recordingModels';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

const entry = (
  state: 'absent' | 'queued' | 'downloading' | 'installed' | 'error',
  bytes = 0,
  est: number | null = null,
  error: string | null = null,
  // The server-computed shared fraction; defaults mirror the backend's
  // progress_of so most snapshots stay terse (queued has no run → null).
  progress: number | null = state === 'installed'
    ? 1
    : state !== 'queued' && est
      ? Math.min(0.99, bytes / est)
      : null,
) => ({
  state,
  bytes_on_disk: bytes,
  size_bytes_estimate: est,
  progress,
  queue_position: state === 'queued' ? 1 : null,
  error,
});

const CATALOG = {
  models: [
    { key: 'parakeet', label: 'Parakeet TDT 0.6B v3' },
    { key: 'whisper', label: 'Whisper Large v3 Turbo' },
    { key: 'ecapa_speaker', label: 'ECAPA Speaker Encoder' },
  ],
};

/** Program api.get: the catalog is static; each /status call shifts the next
 *  snapshot off the queue (the last one repeats once the queue drains). */
function mockApi(statuses: Record<string, unknown>[]) {
  const queue = [...statuses];
  api.get.mockImplementation((url: string) => {
    if (url === '/api/models/catalog') return Promise.resolve(CATALOG);
    if (url === '/api/models/status') {
      return Promise.resolve({ models: queue.length > 1 ? queue.shift() : queue[0] });
    }
    return Promise.reject(new Error(`unexpected GET ${url}`));
  });
}

const setBackend = (backend: string) => {
  useSettingsStore.setState({
    config: { ...useSettingsStore.getState().config, transcriptionBackend: backend },
  });
};

const postedUrls = () => api.post.mock.calls.map((c) => c[0] as string);

beforeEach(() => {
  vi.clearAllMocks();
  api.post.mockResolvedValue({ started: true });
  useUIStore.getState().setModelLoading(null);
  setBackend('streaming');
});

afterEach(() => {
  vi.useRealTimers();
});

describe('requiredRecordingModelKeys', () => {
  it('maps the streaming backend to Parakeet plus the speaker encoder', () => {
    expect(requiredRecordingModelKeys()).toEqual(['parakeet', 'ecapa_speaker']);
  });

  it('maps the whisper backend to Whisper plus the speaker encoder', () => {
    setBackend('whisper');
    expect(requiredRecordingModelKeys()).toEqual(['whisper', 'ecapa_speaker']);
  });
});

describe('ensureRecordingModels', () => {
  it('resolves ready from a single status check when everything is installed', async () => {
    mockApi([
      { parakeet: entry('installed', 1, 1), ecapa_speaker: entry('installed', 1, 1) },
    ]);
    await expect(ensureRecordingModels()).resolves.toBe('ready');
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.post).not.toHaveBeenCalled();
    expect(useUIStore.getState().modelLoading).toBeNull();
  });

  it('proceeds (server fallback) when the status endpoint is unreachable', async () => {
    api.get.mockRejectedValue(new Error('down'));
    await expect(ensureRecordingModels()).resolves.toBe('ready');
    expect(api.post).not.toHaveBeenCalled();
  });

  it('downloads the missing models with banner progress, then resolves ready', async () => {
    vi.useFakeTimers();
    mockApi([
      {
        parakeet: entry('absent', 0, 2_500_000_000),
        ecapa_speaker: entry('absent', 0, 50_000_000),
      },
      {
        // progress is the server's monotonic number — deliberately different
        // from the raw bytes/est ratio (0.42) to pin that the banner renders
        // the shared field, never local arithmetic.
        parakeet: entry('downloading', 1_050_000_000, 2_500_000_000, null, 0.45),
        ecapa_speaker: entry('downloading', 0, 50_000_000),
      },
      {
        parakeet: entry('installed', 2_500_000_000, 2_500_000_000),
        ecapa_speaker: entry('downloading', 25_000_000, 50_000_000),
      },
      {
        parakeet: entry('installed', 2_500_000_000, 2_500_000_000),
        ecapa_speaker: entry('installed', 50_000_000, 50_000_000),
      },
    ]);
    // The speaker encoder hits a benign 409 (slot busy / already running);
    // the poll resolves it without failing the gate.
    api.post.mockImplementation((url: string) =>
      url === '/api/models/ecapa_speaker/download'
        ? Promise.reject(new ApiError(409, 'This model is already downloading.', url, 'POST'))
        : Promise.resolve({ started: true }),
    );

    const gate = ensureRecordingModels();
    await vi.advanceTimersByTimeAsync(0); // flush the initial status/catalog/start chain

    expect(postedUrls()).toEqual([
      '/api/models/parakeet/download',
      '/api/models/ecapa_speaker/download',
    ]);
    let banner = useUIStore.getState().modelLoading;
    expect(banner?.stage).toBe('downloading');
    expect(banner?.label).toBe('Parakeet TDT 0.6B v3');
    expect(banner?.detail).toBe('0 MB of 2.5 GB · model 1 of 2');
    expect(banner?.progress).toBe(0);

    await vi.advanceTimersByTimeAsync(1000);
    banner = useUIStore.getState().modelLoading;
    expect(banner?.detail).toBe('1.1 GB of 2.5 GB · model 1 of 2');
    // The server's shared progress field (0.45), not bytes/est (0.42).
    expect(banner?.progress).toBeCloseTo(0.45, 5);

    await vi.advanceTimersByTimeAsync(1000);
    banner = useUIStore.getState().modelLoading;
    expect(banner?.label).toBe('ECAPA Speaker Encoder');
    expect(banner?.detail).toBe('25 MB of 50 MB · model 2 of 2');

    await vi.advanceTimersByTimeAsync(1000);
    await expect(gate).resolves.toBe('ready');
    expect(useUIStore.getState().modelLoading?.stage).toBe('ready');

    // The ready beat auto-hides.
    await vi.advanceTimersByTimeAsync(700);
    expect(useUIStore.getState().modelLoading).toBeNull();
  });

  it('cancels the downloads and resolves cancelled when the banner Cancel is used', async () => {
    vi.useFakeTimers();
    mockApi([
      { parakeet: entry('absent', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
      {
        parakeet: entry('downloading', 10, 2_500_000_000),
        ecapa_speaker: entry('installed', 1, 1),
      },
    ]);

    const gate = ensureRecordingModels();
    await vi.advanceTimersByTimeAsync(0);

    const banner = useUIStore.getState().modelLoading;
    expect(banner?.stage).toBe('downloading');
    expect(banner?.detail).toBe('0 MB of 2.5 GB'); // single model: no position clause
    banner?.onCancel?.();
    expect(useUIStore.getState().modelLoading).toBeNull(); // hides immediately

    await vi.advanceTimersByTimeAsync(1000);
    await expect(gate).resolves.toBe('cancelled');
    expect(postedUrls()).toContain('/api/models/parakeet/cancel');
  });

  it('shows the failure on the banner and resolves error when a download fails', async () => {
    vi.useFakeTimers();
    mockApi([
      { parakeet: entry('absent', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
      {
        parakeet: entry('error', 0, 2_500_000_000, 'Download worker exited with code 1.'),
        ecapa_speaker: entry('installed', 1, 1),
      },
    ]);

    const gate = ensureRecordingModels();
    await vi.advanceTimersByTimeAsync(1000);
    await expect(gate).resolves.toBe('error');

    const banner = useUIStore.getState().modelLoading;
    expect(banner?.stage).toBe('error');
    expect(banner?.label).toBe('Parakeet TDT 0.6B v3');
    expect(banner?.detail).toBe('Download worker exited with code 1.');

    // The error banner auto-hides.
    await vi.advanceTimersByTimeAsync(8000);
    expect(useUIStore.getState().modelLoading).toBeNull();
  });

  it('shows the waiting banner while queued, then normal progress once downloading', async () => {
    vi.useFakeTimers();
    mockApi([
      { parakeet: entry('absent', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
      // Both server slots are busy with other downloads: the gate's start
      // request queued the model instead of erroring.
      { parakeet: entry('queued', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
      {
        parakeet: entry('downloading', 500_000_000, 2_500_000_000, null, 0.2),
        ecapa_speaker: entry('installed', 1, 1),
      },
      {
        parakeet: entry('installed', 2_500_000_000, 2_500_000_000),
        ecapa_speaker: entry('installed', 1, 1),
      },
    ]);

    const gate = ensureRecordingModels();
    await vi.advanceTimersByTimeAsync(0);
    expect(postedUrls()).toEqual(['/api/models/parakeet/download']);

    await vi.advanceTimersByTimeAsync(1000);
    let banner = useUIStore.getState().modelLoading;
    expect(banner?.stage).toBe('downloading');
    expect(banner?.label).toBe('Parakeet TDT 0.6B v3');
    expect(banner?.detail).toBe('Waiting for other downloads to finish…');
    expect(banner?.progress).toBe(0);

    // The queued download must NOT be restarted — the server pump owns it.
    expect(postedUrls()).toEqual(['/api/models/parakeet/download']);

    await vi.advanceTimersByTimeAsync(1000);
    banner = useUIStore.getState().modelLoading;
    expect(banner?.detail).toBe('500 MB of 2.5 GB');
    expect(banner?.progress).toBeCloseTo(0.2, 5);

    await vi.advanceTimersByTimeAsync(1000);
    await expect(gate).resolves.toBe('ready');
  });

  it('stands down when a QUEUED download is dequeued from Settings > Models', async () => {
    vi.useFakeTimers();
    mockApi([
      { parakeet: entry('absent', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
      { parakeet: entry('queued', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
      // Queued, then gone with no error: the user cancelled it in Settings.
      { parakeet: entry('absent', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
    ]);

    const gate = ensureRecordingModels();
    await vi.advanceTimersByTimeAsync(2000);
    await expect(gate).resolves.toBe('cancelled');
    expect(useUIStore.getState().modelLoading).toBeNull();
  });

  it('stands down when the download disappears (cancelled from Settings > Models)', async () => {
    vi.useFakeTimers();
    mockApi([
      { parakeet: entry('absent', 0, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
      {
        parakeet: entry('downloading', 10, 2_500_000_000),
        ecapa_speaker: entry('installed', 1, 1),
      },
      { parakeet: entry('absent', 10, 2_500_000_000), ecapa_speaker: entry('installed', 1, 1) },
    ]);

    const gate = ensureRecordingModels();
    await vi.advanceTimersByTimeAsync(2000);
    await expect(gate).resolves.toBe('cancelled');
    expect(useUIStore.getState().modelLoading).toBeNull();
  });
});
