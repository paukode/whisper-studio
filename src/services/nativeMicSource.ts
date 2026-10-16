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
 * Bridge contract (pinned; the shell mirrors it):
 *  - marker at documentStart: window.__WHISPER_NATIVE_MIC = { platform, available }
 *  - page -> shell: window.webkit.messageHandlers.nativeMic.postMessage
 *      {cmd:"start"} | {cmd:"stop"}
 *  - shell -> page on window.__whisperNativeMic:
 *      onStarted(deviceName), onAudio(base64, sampleCount, rms),
 *      onError(message), onStopped()
 */
import { decodeNativeAudioChunk } from '@/services/nativeAudioSource';

export interface NativeMicHandlers {
  /** Decoded 16 kHz mono Float32 samples in [-1, 1], with the chunk's RMS. */
  onAudio: (samples: Float32Array, rms?: number) => void;
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
    __WHISPER_NATIVE_MIC?: { platform: string; available: boolean };
    __whisperNativeMic?: NativeMicCallbacks;
  }
}

const STOP_TIMEOUT_MS = 2000;

let pendingStart: { resolve: (device: string) => void; reject: (err: Error) => void } | null =
  null;
let pendingStop: { resolve: () => void; timer: ReturnType<typeof setTimeout> } | null = null;
let activeHandlers: NativeMicHandlers | null = null;

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
    onStarted(device) {
      const pending = pendingStart;
      pendingStart = null;
      pending?.resolve(typeof device === 'string' ? device : '');
    },
    onAudio(base64, sampleCount, rms) {
      const handlers = activeHandlers;
      if (!handlers) return;
      try {
        const level = typeof rms === 'number' && Number.isFinite(rms) ? rms : undefined;
        handlers.onAudio(decodeNativeAudioChunk(base64, sampleCount), level);
      } catch (err) {
        console.error('[native-mic] failed to decode chunk:', err);
      }
    },
    onError(message) {
      const text = typeof message === 'string' && message ? message : 'Microphone error.';
      if (pendingStart) {
        const pending = pendingStart;
        pendingStart = null;
        activeHandlers = null;
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

/** Open the microphone. Resolves with the device name once the shell's
 *  engine runs; rejects on a permission denial or a missing input device.
 *  No timeout: the first start can wait on the OS permission prompt. */
export function startNativeMic(handlers: NativeMicHandlers): Promise<string> {
  ensureCallbacksInstalled();
  if (pendingStart || activeHandlers) {
    return Promise.reject(new Error('The native microphone is already open.'));
  }
  return new Promise<string>((resolve, reject) => {
    pendingStart = { resolve, reject };
    activeHandlers = handlers;
    try {
      postToShell({ cmd: 'start' });
    } catch (err) {
      pendingStart = null;
      activeHandlers = null;
      reject(err instanceof Error ? err : new Error(String(err)));
    }
  });
}

/** Close the microphone. Resolves on the shell's onStopped, or after a short
 *  timeout so teardown never hangs. The shell flushes its last partial chunk
 *  through onAudio before it reports onStopped. */
export function stopNativeMic(): Promise<void> {
  ensureCallbacksInstalled();
  if (pendingStop) return Promise.resolve();
  return new Promise<void>((resolve) => {
    const timer = setTimeout(() => {
      pendingStop = null;
      activeHandlers = null;
      resolve();
    }, STOP_TIMEOUT_MS);
    pendingStop = { resolve, timer };
    try {
      postToShell({ cmd: 'stop' });
    } catch {
      clearTimeout(timer);
      pendingStop = null;
      activeHandlers = null;
      resolve();
    }
  });
}

/** Test hook: clear module state between test cases. */
export function __resetNativeMicForTests(): void {
  if (pendingStop) clearTimeout(pendingStop.timer);
  pendingStart = null;
  pendingStop = null;
  activeHandlers = null;
  if (typeof window !== 'undefined') delete window.__whisperNativeMic;
}
