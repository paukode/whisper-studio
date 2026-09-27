/**
 * Live ASR-engine switch gating.
 *
 * Switching the transcript-panel engine select mid-recording used to relay
 * `set_model` straight to the server, which then downloaded the new engine's
 * weights SILENTLY (no banner) or showed a memory-load ramp stuck at 90%. The
 * controller now gates the new engine's download with the shared banner + a
 * working Cancel BEFORE relaying, and puts the header select back if the
 * download is cancelled/fails so the UI never claims an engine the server
 * isn't running.
 *
 * These tests drive a native-only recording (no getUserMedia / worklet) against
 * the jsdom WebSocket mock, then dispatch the `whisper-set-model` event the
 * real header select fires.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/recordingModels', () => ({
  ensureRecordingModels: vi.fn(async () => 'ready'),
}));
const apiClient = vi.hoisted(() => ({ put: vi.fn(async () => ({})) }));
vi.mock('@/api/client', () => apiClient);

import { recordingController, initRecordingControllerEvents } from './recordingController';
import { ensureRecordingModels } from '@/services/recordingModels';
import { __resetNativeAudioForTests } from './nativeAudioSource';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { answerStopLikeServer } from '@/test/mocks/asrServer';

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));
const native = () => window.__whisperNativeAudio!;

interface TrackedWS {
  _getSentMessages(): (string | ArrayBuffer)[];
  _receiveMessage(data: string | object): void;
  close(): void;
  readyState: number;
  url: string;
}
interface MockSocket {
  readyState: number;
  send(data: string | ArrayBuffer): void;
  close(): void;
  _receiveMessage(data: object): void;
}

let wsInstances: TrackedWS[] = [];
let RealWS: typeof WebSocket;

const setBackend = (backend: string) =>
  useSettingsStore.setState({
    config: { ...useSettingsStore.getState().config, transcriptionBackend: backend },
  });

/** The engine a recording socket asked for when it connected. */
const connectedOn = (w: TrackedWS) => new URL(w.url).searchParams.get('backend');

/** Hold the next model gate open until the test settles it. */
function holdGate(): (result: 'ready' | 'cancelled') => void {
  let settle: (result: 'ready' | 'cancelled') => void = () => {};
  (ensureRecordingModels as ReturnType<typeof vi.fn>).mockImplementationOnce(
    () => new Promise((resolve) => { settle = resolve; }),
  );
  return (result) => settle(result);
}

const relayedBackends = () =>
  wsInstances
    .flatMap((w) => w._getSentMessages())
    .filter((m): m is string => typeof m === 'string')
    .map((m) => {
      try {
        return JSON.parse(m) as { type?: string; backend?: string };
      } catch {
        return {};
      }
    })
    .filter((m) => m.type === 'set_model')
    .map((m) => m.backend);

beforeEach(() => {
  wsInstances = [];
  __resetNativeAudioForTests();
  apiClient.put.mockClear();
  (ensureRecordingModels as ReturnType<typeof vi.fn>).mockResolvedValue('ready');

  // Fake shell bridge: auto-confirm start / stop.
  window.__WHISPER_NATIVE_AUDIO = { platform: 'macos', available: true };
  window.webkit = {
    messageHandlers: {
      nativeAudio: {
        postMessage: (msg: unknown) => {
          const cmd = (msg as { cmd?: string }).cmd;
          if (cmd === 'start') queueMicrotask(() => native().onStarted());
          if (cmd === 'stop') queueMicrotask(() => native().onStopped());
        },
      },
    },
  };

  RealWS = globalThis.WebSocket;
  class TrackingWS extends (RealWS as unknown as { new (url: string): MockSocket }) {
    constructor(url: string) {
      super(url);
      wsInstances.push(this as unknown as TrackedWS);
    }
    send(data: string | ArrayBuffer): void {
      super.send(data);
      answerStopLikeServer(this, data);
    }
  }
  globalThis.WebSocket = TrackingWS as unknown as typeof WebSocket;

  useSessionStore.setState({ saveSession: vi.fn() });
  setBackend('streaming');
  useRecordingStore.setState({
    isRecording: false,
    isConnected: false,
    recordingSessionId: null,
    nativeSource: { pid: -1, name: 'System audio' },
    micEnabled: false,
    activeSourceLabel: null,
  });

  initRecordingControllerEvents();
});

afterEach(async () => {
  vi.useRealTimers();
  if (useRecordingStore.getState().isRecording) {
    recordingController.stop();
    await sleep(300);
  }
  globalThis.WebSocket = RealWS;
  __resetNativeAudioForTests();
  delete window.__WHISPER_NATIVE_AUDIO;
  delete (window as { webkit?: unknown }).webkit;
  vi.clearAllMocks();
});

describe('recordingController — live engine switch gating', () => {
  it('gates the new engine, then relays set_model once its weights are ready', async () => {
    await recordingController.start('switch-ready-session');
    await sleep(10); // let the mocked websocket open
    (ensureRecordingModels as ReturnType<typeof vi.fn>).mockClear();

    // The header select flips config first, then dispatches the event.
    setBackend('whisper');
    window.dispatchEvent(
      new CustomEvent('whisper-set-model', { detail: { backend: 'whisper' } }),
    );
    await sleep(10);

    // The gate ran (downloads Whisper + ecapa with the banner if missing) and
    // ONLY THEN was the switch relayed to the server.
    expect(ensureRecordingModels).toHaveBeenCalledTimes(1);
    expect(relayedBackends()).toEqual(['whisper']);
    // Config stays on the chosen engine.
    expect(useSettingsStore.getState().config.transcriptionBackend).toBe('whisper');
  });

  it('does NOT relay and reverts the select when the download is cancelled', async () => {
    await recordingController.start('switch-cancel-session');
    await sleep(10);
    (ensureRecordingModels as ReturnType<typeof vi.fn>).mockClear();
    (ensureRecordingModels as ReturnType<typeof vi.fn>).mockResolvedValue('cancelled');

    setBackend('whisper');
    window.dispatchEvent(
      new CustomEvent('whisper-set-model', { detail: { backend: 'whisper' } }),
    );
    await sleep(10);

    // Gate ran but the switch was NOT sent — the recording stays on Parakeet.
    expect(ensureRecordingModels).toHaveBeenCalledTimes(1);
    expect(relayedBackends()).toEqual([]);
    // The header select / config is put back to the engine still running, and
    // the revert is persisted.
    expect(useSettingsStore.getState().config.transcriptionBackend).toBe('streaming');
    expect(apiClient.put).toHaveBeenCalledWith('/api/config', { transcription_backend: 'streaming' });
  });

  it('lets the user dismiss the (uncancelable) memory-load banner', async () => {
    await recordingController.start('load-cancel-session');
    await sleep(10);
    const ws = wsInstances[0];
    useUIStore.getState().setModelLoading(null);

    // The server streams a memory-load ramp (start → loading → ready).
    ws._receiveMessage({ type: 'model_loading', stage: 'start', progress: 0, label: 'Whisper' });
    ws._receiveMessage({ type: 'model_loading', stage: 'loading', progress: 0.3, label: 'Whisper' });
    const banner = useUIStore.getState().modelLoading;
    expect(banner?.stage).toBe('loading');
    expect(typeof banner?.onCancel).toBe('function');

    // Cancel hides the banner — the native load can't truly be aborted, so
    // this only stops blocking the user's view.
    banner?.onCancel?.();
    expect(useUIStore.getState().modelLoading).toBeNull();

    // A later ramp tick must NOT re-open the dismissed banner.
    ws._receiveMessage({ type: 'model_loading', stage: 'loading', progress: 0.6, label: 'Whisper' });
    expect(useUIStore.getState().modelLoading).toBeNull();

    // 'ready' still lands (load finished) and re-arms future banners.
    ws._receiveMessage({ type: 'model_loading', stage: 'ready', progress: 1, label: 'Whisper' });
    expect(useUIStore.getState().modelLoading?.stage).toBe('ready');
  });

  it('is a no-op when the engine did not actually change', async () => {
    await recordingController.start('switch-same-session');
    await sleep(10);
    (ensureRecordingModels as ReturnType<typeof vi.fn>).mockClear();

    // Switch to the SAME engine that is already running.
    window.dispatchEvent(
      new CustomEvent('whisper-set-model', { detail: { backend: 'streaming' } }),
    );
    await sleep(10);

    expect(ensureRecordingModels).not.toHaveBeenCalled();
    expect(relayedBackends()).toEqual([]);
  });

  it('a reconnect during the gate reopens on the engine in use, and a cancel leaves it there', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    await recordingController.start('switch-reconnect-session');
    await sleep(10);
    const settleGate = holdGate();

    setBackend('whisper');
    window.dispatchEvent(
      new CustomEvent('whisper-set-model', { detail: { backend: 'whisper' } }),
    );
    // The socket drops while Whisper downloads; the watchdog reconnects.
    wsInstances[0].close();
    vi.advanceTimersByTime(3000);
    await sleep(10);
    expect(wsInstances).toHaveLength(2);
    expect(connectedOn(wsInstances[1])).toBe(connectedOn(wsInstances[0]));
    expect(connectedOn(wsInstances[1])).toBe('streaming');

    settleGate('cancelled');
    await sleep(10);
    expect(relayedBackends()).toEqual([]);
    expect(useSettingsStore.getState().config.transcriptionBackend).toBe('streaming');
  });

  it('a switch picked while the socket is down still waits for the gate', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    await recordingController.start('switch-while-down-session');
    await sleep(10);
    wsInstances[0].close();
    (ensureRecordingModels as ReturnType<typeof vi.fn>).mockClear();
    const settleGate = holdGate();

    setBackend('whisper');
    window.dispatchEvent(
      new CustomEvent('whisper-set-model', { detail: { backend: 'whisper' } }),
    );
    vi.advanceTimersByTime(3000);
    await sleep(10);
    expect(ensureRecordingModels).toHaveBeenCalledTimes(1);
    expect(connectedOn(wsInstances[1])).toBe('streaming');

    // Weights on disk: the open socket switches, and a later reconnect
    // opens on the new engine directly.
    settleGate('ready');
    await sleep(10);
    expect(relayedBackends()).toEqual(['whisper']);
    wsInstances[1].close();
    vi.advanceTimersByTime(3000);
    await sleep(10);
    expect(connectedOn(wsInstances[2])).toBe('whisper');
  });

  it('a gate that finishes while the reconnect is still connecting switches that socket', async () => {
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
    await recordingController.start('switch-connecting-session');
    await sleep(10);
    const settleGate = holdGate();
    setBackend('whisper');
    window.dispatchEvent(
      new CustomEvent('whisper-set-model', { detail: { backend: 'whisper' } }),
    );
    wsInstances[0].close();
    vi.advanceTimersByTime(3000);
    expect(wsInstances[1].readyState).toBe(WebSocket.CONNECTING);

    settleGate('ready');
    for (let i = 0; i < 5; i++) await Promise.resolve();
    expect(wsInstances[1].readyState).toBe(WebSocket.CONNECTING);
    await sleep(10);

    expect(connectedOn(wsInstances[1])).toBe('streaming');
    expect(relayedBackends()).toEqual(['whisper']);
  });
});
