/**
 * Native capture for a source install, which runs in a browser.
 *
 * Inside the Mac app the window injects the bridges nativeAudioSource.ts and
 * nativeMicSource.ts talk to: the markers `window.__WHISPER_NATIVE_AUDIO` /
 * `__WHISPER_NATIVE_MIC`, and `window.webkit.messageHandlers.nativeAudio` /
 * `nativeMic`. In a browser, setup.sh builds the same Swift capture code into
 * a helper program and the server relays it over `/ws/capture-helper`
 * (server/capture_helper.py). This module installs the same globals on top
 * of that WebSocket, so the recording code works unchanged:
 *
 *  - page -> helper: `{channel: "audio"|"mic", ...command}` per message;
 *  - helper -> page: `{channel, event, args}` calls
 *    `window.__whisperNativeAudio[event](...args)` or
 *    `window.__whisperNativeMic[event](...args)`.
 *
 * Does nothing inside the Mac app (its window already has the bridges) and
 * nothing when the server reports no helper (packaged app, or a checkout
 * where setup.sh has not built it).
 */

type Channel = 'audio' | 'mic';

interface HelperMessage {
  channel: Channel;
  event: string;
  args: unknown[];
}

type CallbackTable = Record<string, ((...args: unknown[]) => void) | undefined>;

function callbacksFor(channel: Channel): CallbackTable | undefined {
  const w = window as unknown as Record<string, unknown>;
  return (channel === 'audio' ? w.__whisperNativeAudio : w.__whisperNativeMic) as
    | CallbackTable
    | undefined;
}

/** Deliver one helper line to the page's callback object. */
export function dispatchHelperMessage(raw: string): void {
  let message: HelperMessage;
  try {
    message = JSON.parse(raw) as HelperMessage;
  } catch {
    console.error('[capture-helper] unreadable message:', raw.slice(0, 200));
    return;
  }
  const callback = callbacksFor(message.channel)?.[message.event];
  if (typeof callback !== 'function') return;
  callback(...(Array.isArray(message.args) ? message.args : []));
}

export async function installCaptureHelperBridge(): Promise<void> {
  if (window.webkit?.messageHandlers?.nativeAudio) return;
  const response = await fetch('/api/capture-helper/status');
  if (!response.ok) throw new Error(`capture helper status: HTTP ${response.status}`);
  const status = (await response.json()) as { available?: boolean };
  if (status.available !== true) return;

  const protocol = window.location.protocol === 'https:' ? 'wss:' : 'ws:';
  const socket = new WebSocket(`${protocol}//${window.location.host}/ws/capture-helper`);
  socket.onmessage = (event: MessageEvent) => dispatchHelperMessage(String(event.data));
  socket.onclose = () => {
    // Without the helper the bridges must not look available: the next
    // recording then uses the browser mic, and the source menu hides
    // "This Mac" again.
    window.__WHISPER_NATIVE_AUDIO = { available: false };
    window.__WHISPER_NATIVE_MIC = { available: false };
  };

  const send = (channel: Channel) => (message: unknown) => {
    if (socket.readyState !== WebSocket.OPEN) {
      throw new Error('The capture helper is not connected. Reload the page.');
    }
    socket.send(JSON.stringify({ ...(message as object), channel }));
  };

  await new Promise<void>((resolve, reject) => {
    socket.onopen = () => resolve();
    socket.onerror = () => reject(new Error('The capture helper connection failed.'));
  });

  const w = window as unknown as {
    webkit?: { messageHandlers?: Record<string, { postMessage: (m: unknown) => void }> };
  };
  w.webkit = {
    ...w.webkit,
    messageHandlers: {
      ...w.webkit?.messageHandlers,
      nativeAudio: { postMessage: send('audio') },
      nativeMic: { postMessage: send('mic') },
    },
  };
  window.__WHISPER_NATIVE_AUDIO = { available: true };
  window.__WHISPER_NATIVE_MIC = { available: true };
}
