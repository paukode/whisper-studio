import { beforeEach, describe, expect, it } from 'vitest';
import { createTranscriptionStore } from './transcriptionStore';

describe('applySpeakerUpdates', () => {
  let testStore: ReturnType<typeof createTranscriptionStore>;
  beforeEach(() => {
    testStore = createTranscriptionStore();
  });

  it('relabels a whole segment when all its chunks moved', () => {
    const store = testStore.getState();
    store.addSegment({
      id: 'seg-1', speaker: 'Speaker 1', text: 'Hello there',
      timestamp: 1, edited: false, chunks: [{ id: 0, start: 0 }],
    });
    store.applySpeakerUpdates([{ chunk_id: 0, speaker: 'Speaker 2' }]);
    const segs = testStore.getState().segments;
    expect(segs).toHaveLength(1);
    expect(segs[0].speaker).toBe('Speaker 2');
    expect(segs[0].id).toBe('seg-1');
  });

  it('splits a merged segment at the corrected chunk boundary', () => {
    const store = testStore.getState();
    store.addSegment({
      id: 'seg-1', speaker: 'Speaker 1', text: 'Hello there',
      timestamp: 1, edited: false, chunks: [{ id: 0, start: 0 }],
    });
    // Same provisional speaker, so the recorder merged chunk 1 into seg-1.
    store.appendSegmentText('seg-1', 'General Kenobi', 1);
    store.applySpeakerUpdates([{ chunk_id: 1, speaker: 'Speaker 2' }]);

    const segs = testStore.getState().segments;
    expect(segs).toHaveLength(2);
    expect(segs[0]).toMatchObject({ id: 'seg-1', speaker: 'Speaker 1', text: 'Hello there' });
    expect(segs[1]).toMatchObject({ speaker: 'Speaker 2', text: 'General Kenobi' });
    // Split parts keep usable chunk offsets for any later correction.
    expect(segs[1].chunks).toEqual([{ id: 1, start: 0 }]);
  });

  it('leaves segments without chunk tracking untouched', () => {
    const store = testStore.getState();
    store.addSegment({
      id: 'old', speaker: 'Speaker 1', text: 'Loaded from history',
      timestamp: 1, edited: false,
    });
    store.applySpeakerUpdates([{ chunk_id: 0, speaker: 'Speaker 9' }]);
    expect(testStore.getState().segments[0].speaker).toBe('Speaker 1');
  });

  it('ignores updates for unknown chunk ids', () => {
    const store = testStore.getState();
    store.addSegment({
      id: 'seg-1', speaker: 'Speaker 1', text: 'Hello',
      timestamp: 1, edited: false, chunks: [{ id: 3, start: 0 }],
    });
    store.applySpeakerUpdates([{ chunk_id: 99, speaker: 'Speaker 2' }]);
    expect(testStore.getState().segments[0].speaker).toBe('Speaker 1');
  });
});
