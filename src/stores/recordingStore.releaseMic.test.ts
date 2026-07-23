/**
 * releaseMic must not orphan an in-flight getUserMedia.
 *
 * The shared mic is refcounted: the first acquireMic() opens the device
 * lazily and every owner shares the same pending promise. If the LAST
 * owner releases while that open is still resolving, the store used to
 * null `_micPromise` and find nothing to stop (state has no stream yet) —
 * so the MediaStream getUserMedia eventually handed back stayed live
 * forever (the recording indicator never turned off). These tests pin the
 * fix: the eventual stream is torn down when nobody is left, but preserved
 * when a new owner re-acquires mid-open.
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

describe('recordingStore.releaseMic — in-flight open', () => {
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

  it('keeps the mic alive when a new owner acquires before the open resolves', async () => {
    const store = useRecordingStore.getState();

    const acquire1 = store.acquireMic('owner-1');
    await store.releaseMic('owner-1');

    // A different consumer grabs the mic before the first open lands.
    // `_micPromise` was nulled on release, so this starts a fresh open.
    const acquire2 = store.acquireMic('owner-2');
    expect(getUserMediaMock).toHaveBeenCalledTimes(2);

    // Resolve the FIRST open. A new owner is now live, so its stream must
    // survive — releasing owner-1 must not stop it.
    const stream1 = makeStream();
    gumCalls[0].resolve(stream1);
    const { stream: resolved1 } = await acquire1;
    await flush();

    expect(resolved1).toBe(stream1);
    expect(stream1._track.stop).not.toHaveBeenCalled();

    // Settle the second open so nothing dangles.
    const stream2 = makeStream();
    gumCalls[1].resolve(stream2);
    await acquire2;
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
