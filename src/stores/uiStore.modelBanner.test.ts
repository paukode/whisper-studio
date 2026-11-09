/**
 * The model banner has one slot and four producers (the local chat model,
 * the speech model gate, the recording socket, the dictation socket). A
 * producer shows over whatever is there, but it can only take down its own
 * banner: a delayed "ready" hide or a model_unloaded from one producer must
 * not wipe another's banner and the Cancel that comes with it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { MODEL_BANNER_READY_MS, useUIStore } from './uiStore';

const banner = () => useUIStore.getState().modelLoading;
const open = useUIStore.getState().openModelBanner;

beforeEach(() => {
  vi.useFakeTimers();
  useUIStore.setState({ modelLoading: null });
});

afterEach(() => {
  vi.useRealTimers();
});

describe('model banner ownership', () => {
  it('one producer cannot clear another producer’s banner or its Cancel', () => {
    const recording = open('recording');
    const gate = open('speech-models');
    const cancelDownload = vi.fn();
    recording.show({ label: 'Whisper', progress: 0.3, stage: 'loading' });
    gate.show({ label: 'Parakeet', progress: 0.1, stage: 'downloading', onCancel: cancelDownload });

    recording.clear();
    recording.done();

    expect(banner()?.producer).toBe('speech-models');
    banner()?.onCancel?.();
    expect(cancelDownload).toHaveBeenCalledTimes(1);
  });

  it('a delayed ready hide takes down only that ready banner', () => {
    const dictation = open('dictation');
    const gate = open('speech-models');
    dictation.show({ label: 'Parakeet', progress: 1, stage: 'ready' });
    gate.show({ label: 'Whisper', progress: 0.2, stage: 'downloading', onCancel: vi.fn() });

    vi.advanceTimersByTime(MODEL_BANNER_READY_MS);
    expect(banner()?.producer).toBe('speech-models');

    // Unchallenged, a ready banner hides itself after its beat.
    dictation.show({ label: 'Parakeet', progress: 1, stage: 'ready' });
    vi.advanceTimersByTime(MODEL_BANNER_READY_MS);
    expect(banner()).toBeNull();
  });

  it('a newer flow of the same producer retires the older one', () => {
    const older = open('local-model');
    const newer = open('local-model');
    newer.show({ label: 'Gemma', progress: 0, stage: 'start' });

    // The superseded load winding down neither repaints nor clears.
    older.show({ label: 'Qwen', progress: 0.9, stage: 'loading' });
    older.clear();
    older.done();
    expect(banner()?.label).toBe('Gemma');

    newer.clear();
    expect(banner()).toBeNull();
  });

  it('the newest flow can take down what an older flow of its producer left', () => {
    const droppedSocket = open('recording');
    droppedSocket.show({ label: 'Whisper', progress: 0.4, stage: 'loading' });
    const reconnect = open('recording');
    reconnect.clear();
    expect(banner()).toBeNull();
  });

  it('done keeps the ready beat and hides any other stage at once', () => {
    const load = open('local-model');
    load.show({ label: 'Gemma', progress: 1, stage: 'ready' });
    load.done();
    expect(banner()?.stage).toBe('ready');
    vi.advanceTimersByTime(MODEL_BANNER_READY_MS);
    expect(banner()).toBeNull();

    const failed = open('local-model');
    failed.show({ label: 'Gemma', progress: 0.5, stage: 'loading' });
    failed.done();
    expect(banner()).toBeNull();
  });
});
