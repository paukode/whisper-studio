/**
 * Native microphone: typed wrapper over the macOS shell's mic bridge
 * (macapp/shell/NativeMicCapture.swift).
 *
 * Inside the Mac app the recorder reads the microphone through the shell,
 * not getUserMedia. WebKit opens the mic in "VideoChat" mode (Apple's voice
 * processing), which in field logs took up to 4 s to open, delivered the
 * first ~3 s 18-25 dB too quiet, and fought Teams or Zoom for the audio
 * route during a call. The shell opens the input device directly with voice
 * processing off and streams 16 kHz mono Int16 chunks, the same shape as
 * the system audio tap. A plain browser has no shell and keeps getUserMedia.
 *
 * The recorder and the composer's dictation share the shell's one mic: each
 * acquires it under its own owner name, every owner gets every chunk, and the
 * mic closes when the last owner releases it. Dictation used to open the
 * web view's mic instead, in the voice processing mode that turns other
 * sound on the Mac down for a few seconds.
 *
 * Bridge contract (pinned; the shell mirrors it):
 *  - marker at documentStart: window.__WHISPER_NATIVE_MIC = { available }
 *  - page -> shell: window.webkit.messageHandlers.nativeMic.postMessage
 *      {cmd:"start"} | {cmd:"stop"}
 *  - shell -> page on window.__whisperNativeMic:
 *      onStarted(deviceName), onAudio(base64, sampleCount, rms),
 *      onError(message), onStopped()
 */
import { decodeNativeAudioChunk } from '@/services/nativeAudioSource';

export interface NativeMicHandlers {
  /** Decoded 16 kHz mono Float32 samples in [-1, 1], with the chunk's RMS. */
  onAudio: (samples: Float32Array, rms: number) => void;
  /** The mic failed after it started (the shell has already closed it). */
  onError: (message: string) => void;
  onStopped?: () => void;
}

interface NativeMicCallbacks {
  onStarted: (device: string) => void;
  onAudio: (base64: string, sampleCount: number, rms?: number) => void;
  onError: (message: string) => void;
  onStopped: () => void;
}

declare global {
  interface Window {
    __WHISPER_NATIVE_MIC?: { available: boolean };
    __whisperNativeMic?: NativeMicCallbacks;
  }
}

const STOP_TIMEOUT_MS = 2000;

/** Who holds the mic, each with its handlers. */
const owners = new Map<string, NativeMicHandlers>();
/** The shell's start, until it reports onStarted (or fails). */
let opening: Promise<string> | null = null;
let pendingStart: { resolve: (device: string) => void; reject: (err: Error) => void } | null =
  null;
let pendingStop: { resolve: () => void; timer: ReturnType<typeof setTimeout> } | null = null;
/** The open mic's device name; null while it is closed or still opening. */
let device: string | null = null;

/** Hand the owners over and forget them: the mic ended for all of them. */
function takeOwners(): NativeMicHandlers[] {
  const all = [...owners.values()];
  owners.clear();
  return all;
}

/** True only inside the macOS shell. */
export function isNativeMicAvailable(): boolean {
  return typeof window !== 'undefined' && window.__WHISPER_NATIVE_MIC?.available === true;
}

/** `window.webkit` is declared by nativeAudioSource.ts with its own handler
 *  only; read this bridge's handler through a local cast (see
 *  nativeTranslation.ts for the same pattern). */
function postToShell(message: unknown): void {
  const w = window as unknown as {
    webkit?: { messageHandlers?: { nativeMic?: { postMessage: (m: unknown) => void } } };
  };
  const handler = w.webkit?.messageHandlers?.nativeMic;
  if (!handler) throw new Error('Native microphone bridge is not available.');
  handler.postMessage(message);
}

function ensureCallbacksInstalled(): void {
  if (window.__whisperNativeMic) return;
  window.__whisperNativeMic = {
    onStarted(name) {
      const pending = pendingStart;
      pendingStart = null;
      opening = null;
      device = typeof name === 'string' ? name : '';
      pending?.resolve(device);
      // Every owner left while it opened.
      if (owners.size === 0) void closeShellMic();
    },
    onAudio(base64, sampleCount, rms) {
      if (owners.size === 0) return;
      try {
        const level = typeof rms === 'number' && Number.isFinite(rms) ? rms : 0;
        const samples = decodeNativeAudioChunk(base64, sampleCount);
        for (const handlers of owners.values()) handlers.onAudio(samples, level);
      } catch (err) {
        console.error('[native-mic] failed to decode chunk:', err);
      }
    },
    onError(message) {
      const text = typeof message === 'string' && message ? message : 'Microphone error.';
      device = null;
      if (pendingStart) {
        const pending = pendingStart;
        pendingStart = null;
        opening = null;
        owners.clear();
        pending.reject(new Error(text));
        return;
      }
      for (const handlers of takeOwners()) handlers.onError(text);
    },
    onStopped() {
      const stop = pendingStop;
      pendingStop = null;
      if (stop) {
        // Our own close: its owners were gone. An owner that joined since
        // waits on the start queued after it, so it stays.
        clearTimeout(stop.timer);
        device = null;
        stop.resolve();
        return;
      }
      // The shell stopped it (the device went away): every owner hears it.
      device = null;
      for (const handlers of takeOwners()) handlers.onStopped?.();
    },
  };
}

/** Open the microphone for `owner`, or join it when it is open already.
 *  Resolves with the device name once the shell's engine runs; rejects on a
 *  permission denial or a missing input device (and the owner is let go).
 *  No timeout: the first start can wait on the OS permission prompt. */
export function acquireNativeMic(owner: string, handlers: NativeMicHandlers): Promise<string> {
  ensureCallbacksInstalled();
  owners.set(owner, handlers);
  if (device !== null && !pendingStop) return Promise.resolve(device);
  if (opening) return opening;
  // Closing (or closed): open it again. The shell runs the start after the stop.
  opening = new Promise<string>((resolve, reject) => {
    pendingStart = { resolve, reject };
    try {
      postToShell({ cmd: 'start' });
    } catch (err) {
      pendingStart = null;
      opening = null;
      owners.delete(owner);
      reject(err instanceof Error ? err : new Error(String(err)));
    }
  });
  return opening;
}

/** Let go of the microphone for `owner`. The last owner closes it: resolves
 *  on the shell's onStopped, or after a short timeout so teardown never
 *  hangs. The shell flushes its last partial chunk through onAudio first. */
export function releaseNativeMic(owner: string): Promise<void> {
  if (!owners.delete(owner) || owners.size > 0) return Promise.resolve();
  // Still opening: onStarted closes it, since no owner is left.
  if (opening) return Promise.resolve();
  return closeShellMic();
}

function closeShellMic(): Promise<void> {
  ensureCallbacksInstalled();
  if (pendingStop) return Promise.resolve();
  return new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      pendingStop = null;
      device = null;
      resolve();
    }, STOP_TIMEOUT_MS);
    pendingStop = { resolve, timer };
    try {
      postToShell({ cmd: 'stop' });
    } catch {
      clearTimeout(timer);
      pendingStop = null;
      device = null;
      resolve();
    }
  });
}

/** Test hook: clear module state between test cases. */
export function __resetNativeMicForTests(): void {
  if (pendingStop) clearTimeout(pendingStop.timer);
  pendingStart = null;
  pendingStop = null;
  opening = null;
  device = null;
  owners.clear();
  if (typeof window !== 'undefined') delete window.__whisperNativeMic;
}
