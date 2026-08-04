/**
 * Native audio source — typed wrapper over the macOS shell's WKWebView bridge.
 *
 * The Swift shell (macapp/shell/NativeAudioCapture.swift) captures the Mac's
 * audio OUTPUT with Core Audio process taps (macOS 14.4+): either everything
 * ("System audio", pid -1) or one chosen application. It pushes 16 kHz mono
 * Int16 PCM to the page as base64 chunks; this module decodes them to
 * Float32 so the recording pipeline can mix them with the mic worklet frames.
 *
 * Bridge contract (pinned; the shell mirrors it):
 *  - shell → page marker, injected at documentStart (main frame only):
 *      window.__WHISPER_NATIVE_AUDIO = { platform: "macos", available: bool }
 *    Absence of the object means "not running in the shell".
 *  - page → shell: window.webkit.messageHandlers.nativeAudio.postMessage
 *      {cmd:"list"} | {cmd:"start", pid:-1|N, bundleID?} | {cmd:"stop"}
 *  - shell → page callbacks on window.__whisperNativeAudio:
 *      onSources(json), onStarted(), onAudio(base64, sampleCount, rms?),
 *      onWarning(message), onError(message), onStopped()
 *    rms and onWarning are newer than the rest — both sides treat them as
 *    optional so old shell + new page (and vice versa) keep working.
 */

export interface NativeAudioSourceInfo {
  /** -1 = whole-system audio; otherwise the first process id of one app. */
  pid: number;
  name: string;
  bundleID?: string;
  /** ALL audio pids of the app (helpers included). Newer shells only. */
  pids?: number[];
}

export interface NativeCaptureHandlers {
  /** Decoded 16 kHz mono Float32 samples in [-1, 1]. `rms` is the shell's
   *  root-mean-square level for the chunk in [0, 1] (older shells omit it). */
  onAudio: (samples: Float32Array, rms?: number) => void;
  /** Capture failed mid-stream (the shell has already stopped cleanly). */
  onError: (message: string) => void;
  /** Capture is running but silent (no callbacks / all-zero samples for
   *  5 s). The capture keeps running; this is advisory. */
  onWarning?: (message: string) => void;
  onStopped?: () => void;
}

interface NativeBridgeMarker {
  platform: string;
  available: boolean;
}

interface NativeCallbacks {
  onSources: (sources: NativeAudioSourceInfo[]) => void;
  onStarted: () => void;
  onAudio: (base64: string, sampleCount: number, rms?: number) => void;
  onWarning: (message: string) => void;
  onError: (message: string) => void;
  onStopped: () => void;
}

declare global {
  interface Window {
    __WHISPER_NATIVE_AUDIO?: NativeBridgeMarker;
    __whisperNativeAudio?: NativeCallbacks;
    webkit?: {
      messageHandlers?: {
        nativeAudio?: { postMessage: (message: unknown) => void };
      };
    };
  }
}

const LIST_TIMEOUT_MS = 3000;
const STOP_TIMEOUT_MS = 2000;

// ── Module state ────────────────────────────────────────────────────────
// One capture at a time (mirrors the shell, which replaces any previous
// session on start). Promises are settled by the shell's callbacks.
let pendingList: {
  resolve: (sources: NativeAudioSourceInfo[]) => void;
  reject: (err: Error) => void;
  timer: ReturnType<typeof setTimeout>;
  promise: Promise<NativeAudioSourceInfo[]>;
} | null = null;
let pendingStart: { resolve: () => void; reject: (err: Error) => void } | null = null;
let pendingStop: { resolve: () => void; timer: ReturnType<typeof setTimeout> } | null = null;
let activeHandlers: NativeCaptureHandlers | null = null;

/** True only inside the macOS shell on macOS 14.4+. */
export function isNativeAudioAvailable(): boolean {
  return typeof window !== 'undefined' && window.__WHISPER_NATIVE_AUDIO?.available === true;
}

/** Decode a base64 little-endian Int16 mono chunk into Float32 [-1, 1]. */
export function decodeNativeAudioChunk(base64: string, sampleCount?: number): Float32Array {
  const binary = atob(base64);
  const available = binary.length >> 1;
  const count =
    sampleCount !== undefined && sampleCount >= 0 ? Math.min(sampleCount, available) : available;
  const out = new Float32Array(count);
  for (let i = 0; i < count; i++) {
    // Explicit little-endian decode, independent of platform endianness.
    let v = (binary.charCodeAt(2 * i + 1) << 8) | binary.charCodeAt(2 * i);
    if (v >= 0x8000) v -= 0x10000;
    out[i] = v / 0x8000;
  }
  return out;
}

function postToNative(message: unknown): void {
  const handler = window.webkit?.messageHandlers?.nativeAudio;
  if (!handler) throw new Error('Native audio bridge is not available.');
  handler.postMessage(message);
}

/** Install the shell → page callback object exactly once. The shell calls
 *  these with optional chaining, so late installation is harmless. */
function ensureCallbacksInstalled(): void {
  if (window.__whisperNativeAudio) return;
  window.__whisperNativeAudio = {
    onSources(sources) {
      const pending = pendingList;
      pendingList = null;
      if (!pending) return;
      clearTimeout(pending.timer);
      pending.resolve(Array.isArray(sources) ? sources : []);
    },
    onStarted() {
      const pending = pendingStart;
      pendingStart = null;
      pending?.resolve();
    },
    onAudio(base64, sampleCount, rms) {
      const handlers = activeHandlers;
      if (!handlers) return;
      try {
        const level = typeof rms === 'number' && Number.isFinite(rms) ? rms : undefined;
        handlers.onAudio(decodeNativeAudioChunk(base64, sampleCount), level);
      } catch (err) {
        console.error('[native-audio] failed to decode chunk:', err);
      }
    },
    onWarning(message) {
      // Advisory only (e.g. "capture is silent") — the capture keeps
      // running, so never settle start/stop promises from here.
      const text = typeof message === 'string' && message ? message : 'Native audio warning.';
      try {
        activeHandlers?.onWarning?.(text);
      } catch (err) {
        console.error('[native-audio] onWarning handler failed:', err);
      }
    },
    onError(message) {
      const text = typeof message === 'string' && message ? message : 'Native audio error.';
      // Route by phase: a start awaiting confirmation, then an in-flight
      // list, then a live capture. The shell stops cleanly on any error,
      // so a live capture's handlers are dropped after delivery.
      if (pendingStart) {
        const pending = pendingStart;
        pendingStart = null;
        activeHandlers = null;
        pending.reject(new Error(text));
        return;
      }
      if (pendingList) {
        const pending = pendingList;
        pendingList = null;
        clearTimeout(pending.timer);
        pending.reject(new Error(text));
        return;
      }
      const handlers = activeHandlers;
      activeHandlers = null;
      handlers?.onError(text);
    },
    onStopped() {
      const stop = pendingStop;
      pendingStop = null;
      if (stop) clearTimeout(stop.timer);
      stop?.resolve();
      const handlers = activeHandlers;
      activeHandlers = null;
      handlers?.onStopped?.();
    },
  };
}

/** Ask the shell for the current source list ({pid:-1, "System audio"} plus
 *  apps currently producing audio). Concurrent calls share one round-trip. */
export function listNativeAudioSources(
  timeoutMs = LIST_TIMEOUT_MS,
): Promise<NativeAudioSourceInfo[]> {
  ensureCallbacksInstalled();
  if (pendingList) return pendingList.promise;

  let resolve!: (sources: NativeAudioSourceInfo[]) => void;
  let reject!: (err: Error) => void;
  const promise = new Promise<NativeAudioSourceInfo[]>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  const timer = setTimeout(() => {
    if (pendingList?.promise === promise) pendingList = null;
    reject(new Error('Timed out waiting for the native audio source list.'));
  }, timeoutMs);
  pendingList = { resolve, reject, timer, promise };

  try {
    postToNative({ cmd: 'list' });
  } catch (err) {
    clearTimeout(timer);
    pendingList = null;
    reject(err instanceof Error ? err : new Error(String(err)));
  }
  return promise;
}

/**
 * Start native capture of `pid` (-1 = system audio). Resolves once the shell
 * confirms the tap is running; rejects if the shell reports an error first
 * (including a TCC permission denial). No timeout: the first start can block
 * on the OS permission prompt for as long as the user takes to answer it.
 *
 * `bundleID` (app captures only) lets the shell re-resolve the app at start
 * time — the armed pid may be stale, and one app can span several audio
 * processes (helpers); the shell taps all of them.
 */
export function startNativeCapture(
  pid: number,
  handlers: NativeCaptureHandlers,
  bundleID?: string,
): Promise<void> {
  ensureCallbacksInstalled();
  if (pendingStart || activeHandlers) {
    return Promise.reject(new Error('Native audio capture is already active.'));
  }
  return new Promise<void>((resolve, reject) => {
    pendingStart = {
      resolve: () => resolve(),
      reject: (err) => reject(err),
    };
    activeHandlers = handlers;
    try {
      postToNative(bundleID !== undefined ? { cmd: 'start', pid, bundleID } : { cmd: 'start', pid });
    } catch (err) {
      pendingStart = null;
      activeHandlers = null;
      reject(err instanceof Error ? err : new Error(String(err)));
    }
  });
}

/** Stop any native capture. Resolves on the shell's onStopped (or after a
 *  short fallback timeout so callers never hang on teardown). */
export function stopNativeCapture(): Promise<void> {
  ensureCallbacksInstalled();
  if (pendingStop) {
    // Already stopping — piggyback by resolving immediately after the
    // in-flight stop settles; a second {cmd:"stop"} would be a no-op anyway.
    return Promise.resolve();
  }
  return new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      pendingStop = null;
      activeHandlers = null;
      resolve();
    }, STOP_TIMEOUT_MS);
    pendingStop = { resolve: () => resolve(), timer };
    try {
      postToNative({ cmd: 'stop' });
    } catch {
      clearTimeout(timer);
      pendingStop = null;
      activeHandlers = null;
      resolve();
    }
  });
}

/** Test hook: clear module state between test cases. */
export function __resetNativeAudioForTests(): void {
  if (pendingList) clearTimeout(pendingList.timer);
  if (pendingStop) clearTimeout(pendingStop.timer);
  pendingList = null;
  pendingStart = null;
  pendingStop = null;
  activeHandlers = null;
  if (typeof window !== 'undefined') delete window.__whisperNativeAudio;
}
