/**
 * Mixed-mode recording (mic + native source): the FAIL-OPEN contract.
 *
 * The mic is the clock master; native chunks buffer in NativeAudioMixer and
 * each mic worklet frame pulls its own length from that buffer. These tests
 * pin the invariant that the MICROPHONE keeps reaching the chunker/websocket
 * no matter what the native side does:
 *   - zero native frames ever arriving   → mic passes through unchanged
 *   - native frames stopping mid-recording → mic continues
 *   - the mixer throwing                  → mic continues (mixing disabled)
 * plus the live-level plumbing (shell rms → recordingStore.nativeLevel) and
 * the silent-capture warning toast.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('@/services/recordingModels', () => ({
  ensureRecordingModels: vi.fn(async () => 'ready'),
}));

import { recordingController } from './recordingController';
import { NativeAudioMixer } from './nativeAudioMixer';
import { __resetNativeAudioForTests } from './nativeAudioSource';
import { useRecordingStore } from '@/stores/recordingStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';

/** Base64-encode Int16 samples little-endian, like the Swift shell does. */
function encodeInt16(samples: number[]): string {
  const bytes: number[] = [];
  for (const s of samples) {
    const v = s < 0 ? s + 0x10000 : s;
    bytes.push(v & 0xff, (v >> 8) & 0xff);
  }
  return btoa(String.fromCharCode(...bytes));
}

const native = () => window.__whisperNativeAudio!;
const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

// Default transcription backend is 'streaming' → 320 ms chunks (5120
// samples) — exactly 40 worklet frames of 128 samples.
const CHUNK = 5120;
const FRAME = 128;
const FRAMES_PER_CHUNK = CHUNK / FRAME;

interface TrackedWS {
  _getSentMessages(): (string | ArrayBuffer)[];
  readyState: number;
}

// ── Web Audio fakes (jsdom has neither AudioContext nor AudioWorkletNode) ──

class FakeWorkletPort {
  onmessage: ((e: { data: Float32Array }) => void) | null = null;
}

class FakeAudioWorkletNode {
  port = new FakeWorkletPort();
  constructor() {
    workletNodes.push(this);
  }
  connect(): void {}
  disconnect(): void {}
}

class FakeAudioContext {
  state = 'running';
  destination = {};
  audioWorklet = { addModule: vi.fn(async () => undefined) };
  createMediaStreamSource(): { connect: () => void; disconnect: () => void } {
    return { connect: vi.fn(), disconnect: vi.fn() };
  }
  async close(): Promise<void> {
    this.state = 'closed';
  }
}

let workletNodes: FakeAudioWorkletNode[] = [];
let wsInstances: TrackedWS[] = [];
let posted: unknown[] = [];
let RealWS: typeof WebSocket;
let realAudioContext: unknown;
let realWorkletNode: unknown;

/** Push one mic worklet frame of constant amplitude into the pipeline. */
function feedMicFrame(value = 0.25, length = FRAME): void {
  const node = workletNodes[workletNodes.length - 1];
  expect(node?.port.onmessage).toBeTruthy();
  node.port.onmessage!({ data: new Float32Array(length).fill(value) });
}

function sentBinaries(): ArrayBuffer[] {
  return wsInstances[0]
    ._getSentMessages()
    .filter((m): m is ArrayBuffer => typeof m !== 'string');
}

beforeEach(() => {
  posted = [];
  wsInstances = [];
  workletNodes = [];
  __resetNativeAudioForTests();

  // Fake shell bridge: marker + message handler that auto-confirms.
  window.__WHISPER_NATIVE_AUDIO = { platform: 'macos', available: true };
  window.webkit = {
    messageHandlers: {
      nativeAudio: {
        postMessage: (msg: unknown) => {
          posted.push(msg);
          const cmd = (msg as { cmd?: string }).cmd;
          if (cmd === 'start') queueMicrotask(() => native().onStarted());
          if (cmd === 'stop') queueMicrotask(() => native().onStopped());
        },
      },
    },
  };

  RealWS = globalThis.WebSocket;
  class TrackingWS extends (RealWS as unknown as { new (url: string): object }) {
    constructor(url: string) {
      super(url);
      wsInstances.push(this as unknown as TrackedWS);
    }
  }
  globalThis.WebSocket = TrackingWS as unknown as typeof WebSocket;

  realAudioContext = (globalThis as { AudioContext?: unknown }).AudioContext;
  realWorkletNode = (globalThis as { AudioWorkletNode?: unknown }).AudioWorkletNode;
  (globalThis as { AudioContext: unknown }).AudioContext = FakeAudioContext;
  (globalThis as { AudioWorkletNode: unknown }).AudioWorkletNode = FakeAudioWorkletNode;

  const fakeStream = {
    getTracks: () => [],
    getAudioTracks: () => [],
    getVideoTracks: () => [],
  };
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: vi.fn(async () => fakeStream) },
    configurable: true,
  });

  useSessionStore.setState({ saveSession: vi.fn() });
  useRecordingStore.getState().cleanup();
  useRecordingStore.setState({
    isRecording: false,
    isConnected: false,
    recordingSessionId: null,
    tabStream: null,
    nativeSource: { pid: -1, name: 'System audio' },
    micEnabled: true,
    activeSourceLabel: null,
    nativeLevel: 0,
  });
});

afterEach(async () => {
  if (useRecordingStore.getState().isRecording) {
    recordingController.stop();
    await sleep(300);
  }
  useRecordingStore.getState().cleanup();
  globalThis.WebSocket = RealWS;
  (globalThis as { AudioContext: unknown }).AudioContext = realAudioContext;
  (globalThis as { AudioWorkletNode: unknown }).AudioWorkletNode = realWorkletNode;
  __resetNativeAudioForTests();
  delete window.__WHISPER_NATIVE_AUDIO;
  delete (window as { webkit?: unknown }).webkit;
  vi.restoreAllMocks();
  vi.clearAllMocks();
});

describe('recordingController — mixed mode fail-open', () => {
  it('mic passes through unchanged when NO native frames ever arrive', async () => {
    await recordingController.start('mixed-no-native');
    await sleep(10); // let the mocked websocket open

    expect(posted).toContainEqual({ cmd: 'start', pid: -1 });
    expect(useRecordingStore.getState().activeSourceLabel).toBe('Mic + System audio');

    // The shell never delivers a single chunk — permission silently broken.
    for (let i = 0; i < FRAMES_PER_CHUNK; i++) feedMicFrame(0.25);

    const binary = sentBinaries();
    expect(binary).toHaveLength(1);
    expect(binary[0].byteLength).toBe(CHUNK * 2);
    const pcm = new Int16Array(binary[0]);
    // Pure mic: every sample ≈ 0.25 (mixed with silence, unchanged).
    expect(pcm[0]).toBeGreaterThan(0x1f00);
    expect(pcm[0]).toBeLessThanOrEqual(0x2000);
    expect(pcm[pcm.length - 1]).toBeGreaterThan(0x1f00);
  });

  it('mic continues when native frames stop mid-recording', async () => {
    await recordingController.start('mixed-native-stops');
    await sleep(10);

    // 1600 native samples of 0.25 arrive, then the native side goes dead.
    native().onAudio(encodeInt16(new Array<number>(1600).fill(0x2000)), 1600, 0.25);

    for (let i = 0; i < FRAMES_PER_CHUNK; i++) feedMicFrame(0.25);
    // Native silence from here on — only mic frames.
    for (let i = 0; i < FRAMES_PER_CHUNK; i++) feedMicFrame(0.25);

    const binary = sentBinaries();
    expect(binary).toHaveLength(2);
    const first = new Int16Array(binary[0]);
    const second = new Int16Array(binary[1]);
    // First chunk head: mic 0.25 + native 0.25 ≈ 0.5.
    expect(first[0]).toBeGreaterThan(0x3f00);
    // First chunk tail (past the 1600 mixed samples): mic only.
    expect(first[first.length - 1]).toBeLessThanOrEqual(0x2000);
    // Second chunk: native long gone, mic still flowing at full length.
    expect(second).toHaveLength(CHUNK);
    expect(second[0]).toBeGreaterThan(0x1f00);
    expect(second[0]).toBeLessThanOrEqual(0x2000);
  });

  it('mic continues when the mixer throws (fail-open), mixing disabled', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    const mixInto = vi
      .spyOn(NativeAudioMixer.prototype, 'mixInto')
      .mockImplementation(() => {
        throw new Error('mixer exploded');
      });

    await recordingController.start('mixed-mixer-throws');
    await sleep(10);

    native().onAudio(encodeInt16(new Array<number>(1600).fill(0x2000)), 1600);
    for (let i = 0; i < FRAMES_PER_CHUNK; i++) feedMicFrame(0.25);

    const binary = sentBinaries();
    expect(binary).toHaveLength(1);
    const pcm = new Int16Array(binary[0]);
    expect(pcm).toHaveLength(CHUNK);
    // Raw mic frames — the poisoned mixer was bypassed entirely.
    expect(pcm[0]).toBeGreaterThan(0x1f00);
    expect(pcm[0]).toBeLessThanOrEqual(0x2000);
    // Mixer was tried once, then dropped: later frames never touch it.
    expect(mixInto).toHaveBeenCalledTimes(1);
    expect(consoleError).toHaveBeenCalled();
    expect(useRecordingStore.getState().isRecording).toBe(true);
  });

  it('a native push failure drops the chunk but never the mic', async () => {
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {});
    vi.spyOn(NativeAudioMixer.prototype, 'push').mockImplementation(() => {
      throw new Error('push exploded');
    });

    await recordingController.start('mixed-push-throws');
    await sleep(10);

    expect(() =>
      native().onAudio(encodeInt16(new Array<number>(1600).fill(0x2000)), 1600),
    ).not.toThrow();
    for (let i = 0; i < FRAMES_PER_CHUNK; i++) feedMicFrame(0.25);

    expect(sentBinaries()).toHaveLength(1);
    expect(consoleError).toHaveBeenCalled();
    expect(useRecordingStore.getState().isRecording).toBe(true);
  });

  it('feeds the shell rms into recordingStore.nativeLevel and clears it on stop', async () => {
    await recordingController.start('mixed-level');
    await sleep(10);

    native().onAudio(encodeInt16(new Array<number>(1600).fill(0x2000)), 1600, 0.31);
    expect(useRecordingStore.getState().nativeLevel).toBeCloseTo(0.31, 3);

    // Old-shell chunks without rms fall back to a computed level.
    native().onAudio(encodeInt16(new Array<number>(1600).fill(0)), 1600);
    expect(useRecordingStore.getState().nativeLevel).toBe(0);

    native().onAudio(encodeInt16(new Array<number>(1600).fill(0x2000)), 1600, 0.5);
    expect(useRecordingStore.getState().nativeLevel).toBeCloseTo(0.5, 3);

    recordingController.stop();
    await sleep(300);
    expect(useRecordingStore.getState().nativeLevel).toBe(0);
  });

  it('shows a warning toast (bell-persisted) when the shell reports silent capture', async () => {
    await recordingController.start('mixed-silent-warning');
    await sleep(10);

    const before = useUIStore.getState().toasts.length;
    native().onWarning(
      'System audio capture is producing no sound. Check System Settings > Privacy & Security > Screen & System Audio Recording, then stop and re-start the source.',
    );
    const toasts = useUIStore.getState().toasts;
    expect(toasts.length).toBe(before + 1);
    const toast = toasts[toasts.length - 1];
    expect(toast.type).toBe('warning');
    expect(toast.message).toMatch(/Screen & System Audio Recording/);
    // The capture did NOT stop — warning is advisory.
    expect(useRecordingStore.getState().isRecording).toBe(true);
    expect(useRecordingStore.getState().activeSourceLabel).toBe('Mic + System audio');
  });
});
