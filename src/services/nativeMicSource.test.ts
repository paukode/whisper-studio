import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { __resetNativeMicForTests, acquireNativeMic, releaseNativeMic } from './nativeMicSource';

/** Every command the page posted to the shell's mic bridge. */
let posted: string[] = [];
/** When false, the test answers the shell's events itself. */
let autoAnswer = true;

function chunk(samples: number, value: number): string {
  const pcm = new Int16Array(samples).fill(value);
  let binary = '';
  for (const byte of new Uint8Array(pcm.buffer)) binary += String.fromCharCode(byte);
  return btoa(binary);
}

const shell = () => window.__whisperNativeMic!;
const handlers = () => ({ onAudio: vi.fn(), onError: vi.fn(), onStopped: vi.fn() });

beforeEach(() => {
  posted = [];
  autoAnswer = true;
  __resetNativeMicForTests();
  window.__WHISPER_NATIVE_MIC = { available: true };
  (window as unknown as { webkit: unknown }).webkit = {
    messageHandlers: {
      nativeMic: {
        postMessage: (msg: { cmd: string }) => {
          posted.push(msg.cmd);
          if (!autoAnswer) return;
          if (msg.cmd === 'start') queueMicrotask(() => shell().onStarted('MacBook Pro Microphone'));
          if (msg.cmd === 'stop') queueMicrotask(() => shell().onStopped());
        },
      },
    },
  };
});

afterEach(() => {
  __resetNativeMicForTests();
  delete window.__WHISPER_NATIVE_MIC;
});

describe('the shared native mic', () => {
  it('opens once for two owners, gives each every chunk, and closes with the last one', async () => {
    const recorder = handlers();
    const dictation = handlers();

    expect(await acquireNativeMic('header-recorder', recorder)).toBe('MacBook Pro Microphone');
    expect(await acquireNativeMic('chat-input-mic', dictation)).toBe('MacBook Pro Microphone');
    expect(posted).toEqual(['start']);

    shell().onAudio(chunk(4, 1000), 4, 0.1);
    expect(recorder.onAudio).toHaveBeenCalledTimes(1);
    expect(dictation.onAudio).toHaveBeenCalledTimes(1);

    await releaseNativeMic('chat-input-mic');
    expect(posted).toEqual(['start']);
    shell().onAudio(chunk(4, 1000), 4, 0.1);
    expect(recorder.onAudio).toHaveBeenCalledTimes(2);
    expect(dictation.onAudio).toHaveBeenCalledTimes(1);

    await releaseNativeMic('header-recorder');
    expect(posted).toEqual(['start', 'stop']);
  });

  it('a second owner joins a start still in flight', async () => {
    autoAnswer = false;
    const first = acquireNativeMic('header-recorder', handlers());
    const second = acquireNativeMic('chat-input-mic', handlers());
    shell().onStarted('Mic');

    expect(await first).toBe('Mic');
    expect(await second).toBe('Mic');
    expect(posted).toEqual(['start']);
  });

  it('an owner that comes back while the mic closes keeps it, and gets chunks again', async () => {
    autoAnswer = false;
    const dictation = handlers();
    const opened = acquireNativeMic('chat-input-mic', dictation);
    shell().onStarted('Mic');
    await opened;

    const closing = releaseNativeMic('chat-input-mic');
    const again = acquireNativeMic('chat-input-mic', dictation);
    shell().onStopped();
    await closing;
    shell().onStarted('Mic');

    expect(await again).toBe('Mic');
    expect(posted).toEqual(['start', 'stop', 'start']);
    shell().onAudio(chunk(4, 1000), 4, 0.1);
    expect(dictation.onAudio).toHaveBeenCalledTimes(1);
    expect(dictation.onStopped).not.toHaveBeenCalled();
  });

  it('closes a mic that opens after every owner left', async () => {
    autoAnswer = false;
    const pending = acquireNativeMic('chat-input-mic', handlers());
    await releaseNativeMic('chat-input-mic');
    shell().onStarted('Mic');
    await pending;

    expect(posted).toEqual(['start', 'stop']);
  });

  it('tells every owner when the device goes away', async () => {
    const recorder = handlers();
    const dictation = handlers();
    await acquireNativeMic('header-recorder', recorder);
    await acquireNativeMic('chat-input-mic', dictation);

    shell().onError('The input device was disconnected.');

    expect(recorder.onError).toHaveBeenCalledWith('The input device was disconnected.');
    expect(dictation.onError).toHaveBeenCalledWith('The input device was disconnected.');
  });
});
