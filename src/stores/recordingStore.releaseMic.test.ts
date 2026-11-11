/**
 * The shared mic stops every stream it opens once no owner holds it.
 *
 * The mic is refcounted: the first acquireMic() opens the device lazily and
 * every owner shares the same pending open. Owners can let go while that
 * open is still resolving, and a new owner can arrive in the same window.
 * These tests pin the contract for each order: an owner that holds the mic
 * keeps a live stream, and once nobody holds it, every stream getUserMedia
 * handed back has been stopped (none is left live with no owner to stop it).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { useRecordingStore } from './recordingStore';

// The shape acquireMic resolves to: { stream, context }.
type FakeTrack = { stop: ReturnType<typeof vi.fn> };
interface FakeStream {
  _track: FakeTrack;
  getTracks: () => FakeTrack[];
}

function makeStream(): FakeStream {
  const track: FakeTrack = { stop: vi.fn() };
  return { _track: track, getTracks: () => [track] };
}

// One deferred per getUserMedia call, so tests can drive multiple opens
// independently (last-owner-release then a fresh re-acquire).
interface Deferred {
  promise: Promise<FakeStream>;
  resolve: (stream: FakeStream) => void;
  reject: (err: unknown) => void;
}

let gumCalls: Deferred[] = [];

const getUserMediaMock = vi.fn(() => {
  let resolve!: (stream: FakeStream) => void;
  let reject!: (err: unknown) => void;
  const promise = new Promise<FakeStream>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  gumCalls.push({ promise, resolve, reject });
  return promise;
});

// Minimal AudioContext stand-in — jsdom has none. Tracks close() so we can
// assert the context is torn down alongside the stream.
class FakeAudioContext {
  state: 'running' | 'closed' = 'running';
  close = vi.fn(() => {
    this.state = 'closed';
    return Promise.resolve();
  });
}

// A macrotask flush guarantees the microtask cleanup chained onto the
// pending open has run before we assert.
const flush = () => new Promise<void>((resolve) => setTimeout(resolve, 0));

beforeEach(() => {
  gumCalls = [];
  getUserMediaMock.mockClear();
  Object.defineProperty(navigator, 'mediaDevices', {
    value: { getUserMedia: getUserMediaMock },
    configurable: true,
    writable: true,
  });
  vi.stubGlobal('AudioContext', FakeAudioContext);
  // Reset the module-level refcount + shared promise between tests.
  useRecordingStore.getState().cleanup();
});

afterEach(() => {
  useRecordingStore.getState().cleanup();
  vi.unstubAllGlobals();
});

describe('recordingStore.releaseMic: in-flight open', () => {
  it('stops the orphaned mic when the last owner releases before getUserMedia resolves', async () => {
    const store = useRecordingStore.getState();

    // Acquire; getUserMedia is deferred, so `_micPromise` is still pending
    // and state has no stream yet.
    const acquire = store.acquireMic('recorder');
    expect(getUserMediaMock).toHaveBeenCalledTimes(1);
    expect(useRecordingStore.getState().micStream).toBeNull();

    // Release as the ONLY owner while the open is still in flight.
    await store.releaseMic('recorder');

    // Now the device finishes opening.
    const stream = makeStream();
    gumCalls[0].resolve(stream);
    const { stream: resolved, context } = await acquire;
    await flush();

    // The resolved-but-orphaned stream must be torn down: no live mic left.
    expect(resolved).toBe(stream);
    expect(stream._track.stop).toHaveBeenCalledTimes(1);
    expect((context as unknown as FakeAudioContext).close).toHaveBeenCalledTimes(1);
    expect(useRecordingStore.getState().micStream).toBeNull();
    expect(useRecordingStore.getState().audioContext).toBeNull();
  });

  it('a new owner arriving mid-open shares the open, and its release stops the stream', async () => {
    const store = useRecordingStore.getState();

    const acquire1 = store.acquireMic('owner-1');
    await store.releaseMic('owner-1');

    // A different consumer grabs the mic before the first open lands. It
    // joins the pending open instead of starting a second getUserMedia.
    const acquire2 = store.acquireMic('owner-2');
    const stream = makeStream();
    gumCalls[0].resolve(stream);
    const [{ stream: got1 }, { stream: got2 }] = await Promise.all([acquire1, acquire2]);
    await flush();

    expect(getUserMediaMock).toHaveBeenCalledTimes(1);
    expect(got2).toBe(got1);
    // Held by owner-2: live.
    expect(stream._track.stop).not.toHaveBeenCalled();
    expect(useRecordingStore.getState().micStream).toBe(stream);

    await store.releaseMic('owner-2');
    expect(stream._track.stop).toHaveBeenCalledTimes(1);
    expect(useRecordingStore.getState().micStream).toBeNull();
  });

  it('a second getUserMedia stream is stopped too: no stream outlives its owners', async () => {
    const store = useRecordingStore.getState();

    // cleanup() drops a pending open, so the next owner opens the device
    // again: two getUserMedia calls are in flight at once.
    const acquire1 = store.acquireMic('owner-1');
    store.cleanup();
    const acquire2 = store.acquireMic('owner-2');
    expect(getUserMediaMock).toHaveBeenCalledTimes(2);

    // The dropped open lands after the second, while owner-2 holds the mic.
    const streams = [makeStream(), makeStream()];
    gumCalls[1].resolve(streams[1]);
    gumCalls[0].resolve(streams[0]);
    await Promise.all([acquire1, acquire2]);
    await flush();

    // owner-2's stream is the live one; the dropped open's stream is not.
    expect(useRecordingStore.getState().micStream).toBe(streams[1]);
    expect(streams[1]._track.stop).not.toHaveBeenCalled();
    expect(streams[0]._track.stop).toHaveBeenCalledTimes(1);

    await store.releaseMic('owner-2');
    for (const s of streams) expect(s._track.stop).toHaveBeenCalledTimes(1);
  });

  it('stops the stream when the AudioContext cannot be made', async () => {
    vi.stubGlobal(
      'AudioContext',
      class {
        constructor() {
          throw new Error('no audio context');
        }
      },
    );
    const store = useRecordingStore.getState();
    const acquire = store.acquireMic('recorder');
    const stream = makeStream();
    gumCalls[0].resolve(stream);

    await expect(acquire).rejects.toThrow('no audio context');
    expect(stream._track.stop).toHaveBeenCalledTimes(1);
    // The failed open does not lock the store: the next acquire opens again.
    void store.acquireMic('recorder');
    expect(getUserMediaMock).toHaveBeenCalledTimes(2);
  });

  it('still stops tracks on the normal path (release after the stream is resolved)', async () => {
    const store = useRecordingStore.getState();

    const acquire = store.acquireMic('recorder');
    const stream = makeStream();
    gumCalls[0].resolve(stream);
    const { context } = await acquire;

    // Stream is fully resolved and in state before release.
    expect(useRecordingStore.getState().micStream).not.toBeNull();

    await store.releaseMic('recorder');

    expect(stream._track.stop).toHaveBeenCalledTimes(1);
    expect((context as unknown as FakeAudioContext).close).toHaveBeenCalledTimes(1);
    expect(useRecordingStore.getState().micStream).toBeNull();
    expect(useRecordingStore.getState().audioContext).toBeNull();
  });
});
