/**
 * Native translation — typed wrapper over the macOS shell's WKWebView bridge
 * to Apple's on-device Translation framework.
 *
 * Bridge contract (pinned; macapp/shell/NativeTranslation.swift mirrors it):
 *  - shell → page marker, injected at documentStart (main frame only):
 *      window.__WHISPER_NATIVE_TRANSLATE = { available: bool }
 *    Absence means "not running in the shell" (or macOS < 15).
 *  - page → shell: window.webkit.messageHandlers.nativeTranslate.postMessage
 *      {id: string, text: string, source: string, target: string}
 *  - shell → page callback:
 *      window.__whisperNativeTranslate.onResult(id, text|null, error|null)
 *
 * The Translation framework downloads its language models on first use (the
 * OS shows a one-time consent sheet); after that everything is offline.
 */

interface NativeTranslateMarker {
  available: boolean;
}

declare global {
  interface Window {
    __WHISPER_NATIVE_TRANSLATE?: NativeTranslateMarker;
    __whisperNativeTranslate?: {
      onResult: (id: string, text: string | null, error: string | null) => void;
    };
  }
}

/** `window.webkit` is already globally declared by nativeAudioSource.ts with
 *  only its own handler; TS interface merging forbids re-declaring the
 *  property with a different shape, so this bridge reads its handler via a
 *  local cast instead. */
function nativeTranslateHandler(): { postMessage: (message: unknown) => void } | undefined {
  const w = window as unknown as {
    webkit?: { messageHandlers?: { nativeTranslate?: { postMessage: (m: unknown) => void } } };
  };
  return w.webkit?.messageHandlers?.nativeTranslate;
}

/** Reject a translation that never comes back (shell hiccup, model download
 *  declined). Generous because the first request may wait on the OS model
 *  download prompt. */
const TIMEOUT_MS = 60_000;

const pending = new Map<string, { resolve: (text: string) => void; timer: number }>();

function ensureCallback(): void {
  if (window.__whisperNativeTranslate) return;
  window.__whisperNativeTranslate = {
    onResult: (id, text, error) => {
      const entry = pending.get(id);
      if (!entry) return;
      pending.delete(id);
      window.clearTimeout(entry.timer);
      if (error) {
        console.warn('[translate] native translation failed:', error);
        entry.resolve('');
      } else {
        entry.resolve(text ?? '');
      }
    },
  };
}

export function isNativeTranslationAvailable(): boolean {
  return window.__WHISPER_NATIVE_TRANSLATE?.available === true && !!nativeTranslateHandler();
}

/** Translate one segment on-device. An empty `source` asks the framework to
 *  auto-detect the language (used for Parakeet, which does no language ID);
 *  same-language input then fails and resolves to '' like any other failure,
 *  so callers can always clear the pending placeholder with the result. */
export function translateNative(text: string, source: string, target = 'en'): Promise<string> {
  const handler = nativeTranslateHandler();
  if (!handler || !isNativeTranslationAvailable()) return Promise.resolve('');
  ensureCallback();
  const id = crypto.randomUUID();
  return new Promise<string>((resolve) => {
    const timer = window.setTimeout(() => {
      pending.delete(id);
      console.warn('[translate] native translation timed out');
      resolve('');
    }, TIMEOUT_MS);
    pending.set(id, { resolve, timer });
    try {
      handler.postMessage({ id, text, source, target });
    } catch (err) {
      pending.delete(id);
      window.clearTimeout(timer);
      console.warn('[translate] native bridge post failed:', err);
      resolve('');
    }
  });
}
