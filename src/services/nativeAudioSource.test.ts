/**
 * nativeAudioSource — bridge-contract tests against a fake shell.
 *
 * The fake installs window.__WHISPER_NATIVE_AUDIO (the documentStart marker)
 * and window.webkit.messageHandlers.nativeAudio (page → native), and drives
 * the native → page callbacks through window.__whisperNativeAudio exactly
 * like macapp/shell/NativeAudioCapture.swift does.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  __resetNativeAudioForTests,
  decodeNativeAudioChunk,
  isNativeAudioAvailable,
  listNativeAudioSources,
  startNativeCapture,
  stopNativeCapture,
} from './nativeAudioSource';

/** Base64-encode Int16 samples little-endian, like the Swift shell does. */
function encodeInt16(samples: number[]): string {
  const bytes: number[] = [];
  for (const s of samples) {
    const v = s < 0 ? s + 0x10000 : s;
    bytes.push(v & 0xff, (v >> 8) & 0xff);
  }
  return btoa(String.fromCharCode(...bytes));
}

let posted: unknown[] = [];

function installFakeBridge(available = true): void {
  window.__WHISPER_NATIVE_AUDIO = { platform: 'macos', available };
  window.webkit = {
    messageHandlers: {
      nativeAudio: {
        postMessage: (msg: unknown) => {
          posted.push(msg);
        },
      },
    },
  };
}

/** The callbacks object the module installs for the shell to call. */
const native = () => window.__whisperNativeAudio!;

const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  posted = [];
  __resetNativeAudioForTests();
  installFakeBridge();
});

afterEach(() => {
  __resetNativeAudioForTests();
  delete window.__WHISPER_NATIVE_AUDIO;
  delete (window as { webkit?: unknown }).webkit;
});

describe('isNativeAudioAvailable', () => {
  it('is true only when the shell marker says available', () => {
    expect(isNativeAudioAvailable()).toBe(true);
    installFakeBridge(false);
    expect(isNativeAudioAvailable()).toBe(false);
    delete window.__WHISPER_NATIVE_AUDIO;
    expect(isNativeAudioAvailable()).toBe(false);
  });
});

describe('decodeNativeAudioChunk', () => {
  it('decodes little-endian Int16 to Float32 in [-1, 1]', () => {
    const b64 = encodeInt16([-32768, 0, 16384, 32767]);
    const out = decodeNativeAudioChunk(b64, 4);
    expect(out).toHaveLength(4);
    expect(out[0]).toBeCloseTo(-1, 5);
    expect(out[1]).toBeCloseTo(0, 5);
    expect(out[2]).toBeCloseTo(0.5, 5);
    expect(out[3]).toBeCloseTo(32767 / 32768, 5);
  });

  it('never reads past the payload when sampleCount over-claims', () => {
    const b64 = encodeInt16([100, 200]);
    expect(decodeNativeAudioChunk(b64, 10)).toHaveLength(2);
  });
});

describe('listNativeAudioSources', () => {
  it('posts {cmd:"list"} and resolves with the onSources payload', async () => {
    const promise = listNativeAudioSources();
    expect(posted).toEqual([{ cmd: 'list' }]);
    native().onSources([
      { pid: -1, name: 'System audio' },
      { pid: 123, name: 'Zoom', bundleID: 'us.zoom.xos' },
    ]);
    await expect(promise).resolves.toEqual([
      { pid: -1, name: 'System audio' },
      { pid: 123, name: 'Zoom', bundleID: 'us.zoom.xos' },
    ]);
  });

  it('shares one round-trip between concurrent callers', async () => {
    const a = listNativeAudioSources();
    const b = listNativeAudioSources();
    expect(posted).toHaveLength(1);
    native().onSources([{ pid: -1, name: 'System audio' }]);
    await expect(a).resolves.toHaveLength(1);
    await expect(b).resolves.toHaveLength(1);
  });

  it('rejects on timeout', async () => {
    await expect(listNativeAudioSources(10)).rejects.toThrow(/timed out/i);
  });

  it('rejects when the shell reports an error', async () => {
    const promise = listNativeAudioSources();
    native().onError('requires macOS 14.4');
    await expect(promise).rejects.toThrow('requires macOS 14.4');
  });
});

describe('startNativeCapture / stopNativeCapture', () => {
  it('posts {cmd:"start", pid} and resolves on onStarted', async () => {
    const onAudio = vi.fn();
    const promise = startNativeCapture(-1, { onAudio, onError: vi.fn() });
    expect(posted).toEqual([{ cmd: 'start', pid: -1 }]);
    native().onStarted();
    await expect(promise).resolves.toBeUndefined();
  });

  it('rejects when onError arrives before onStarted (e.g. TCC denial)', async () => {
    const promise = startNativeCapture(42, { onAudio: vi.fn(), onError: vi.fn() });
    native().onError('permission denied');
    await expect(promise).rejects.toThrow('permission denied');
    // The failed start cleared the handlers — a fresh start must work.
    const retry = startNativeCapture(42, { onAudio: vi.fn(), onError: vi.fn() });
    native().onStarted();
    await expect(retry).resolves.toBeUndefined();
  });

  it('delivers decoded Float32 chunks to onAudio', async () => {
    const frames: Float32Array[] = [];
    const promise = startNativeCapture(-1, {
      onAudio: (samples) => frames.push(samples),
      onError: vi.fn(),
    });
    native().onStarted();
    await promise;
    native().onAudio(encodeInt16([16384, -16384]), 2);
    expect(frames).toHaveLength(1);
    expect(frames[0][0]).toBeCloseTo(0.5, 5);
    expect(frames[0][1]).toBeCloseTo(-0.5, 5);
  });

  it('routes a mid-capture error to onError and stops delivering audio', async () => {
    const onAudio = vi.fn();
    const onError = vi.fn();
    const promise = startNativeCapture(-1, { onAudio, onError });
    native().onStarted();
    await promise;
    native().onError('tap died');
    expect(onError).toHaveBeenCalledWith('tap died');
    native().onAudio(encodeInt16([1]), 1);
    expect(onAudio).not.toHaveBeenCalled();
  });

  it('refuses a second start while one capture is active', async () => {
    const first = startNativeCapture(-1, { onAudio: vi.fn(), onError: vi.fn() });
    native().onStarted();
    await first;
    await expect(startNativeCapture(7, { onAudio: vi.fn(), onError: vi.fn() })).rejects.toThrow(
      /already active/i,
    );
  });

  it('stop posts {cmd:"stop"}, resolves on onStopped and fires onStopped handler', async () => {
    const onStopped = vi.fn();
    const started = startNativeCapture(-1, {
      onAudio: vi.fn(),
      onError: vi.fn(),
      onStopped,
    });
    native().onStarted();
    await started;
    const stopping = stopNativeCapture();
    expect(posted).toContainEqual({ cmd: 'stop' });
    native().onStopped();
    await expect(stopping).resolves.toBeUndefined();
    await flush();
    expect(onStopped).toHaveBeenCalledTimes(1);
  });

  it('rejects start when the message handler is missing', async () => {
    delete (window as { webkit?: unknown }).webkit;
    await expect(startNativeCapture(-1, { onAudio: vi.fn(), onError: vi.fn() })).rejects.toThrow(
      /not available/i,
    );
  });
});
