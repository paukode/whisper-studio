/**
 * Each transcript segment shows the time its first words arrived.
 *
 * Field report (2026-10-22): every segment of a 54-minute recording showed
 * the same time. The server sends each sentence at once under a provisional
 * label (the last labelled speaker) and corrects it 2 to 3 s later. A
 * sentence under the same label as the last segment grows that segment, and
 * the correction splits it off again. The split-off part copied the
 * segment's timestamp, so from the first sentence on, every segment was a
 * split part carrying the time of the first sentence.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createTranscriptionStore, persistedSegments } from './transcriptionStore';

const T0 = Date.UTC(2026, 9, 22, 8, 5, 47);

describe('split segments keep their own arrival time', () => {
  let store: ReturnType<typeof createTranscriptionStore>;
  beforeEach(() => {
    vi.useFakeTimers();
    vi.setSystemTime(T0);
    store = createTranscriptionStore();
  });
  afterEach(() => {
    vi.useRealTimers();
  });

  /** What recordingController.onTranscriptResult does with one final. */
  function finalArrives(chunkId: number, text: string, speaker: string) {
    const s = store.getState();
    const last = s.segments[s.segments.length - 1];
    if (last && last.speaker === speaker) {
      s.appendSegmentText(last.id, text, chunkId);
    } else {
      const now = Date.now();
      s.addSegment({
        id: `seg-${chunkId}`, speaker, text, timestamp: now, edited: false,
        chunks: [{ id: chunkId, start: 0, at: now }],
      });
    }
  }

  it('stamps a part split off by a speaker correction with its first chunk', () => {
    finalArrives(0, 'Good morning.', 'Speaker 1');
    vi.setSystemTime(T0 + 4_000);
    finalArrives(1, 'Morning, all.', 'Speaker 1');
    vi.setSystemTime(T0 + 9_000);
    finalArrives(2, 'Shall we start?', 'Speaker 1');
    store.getState().applySpeakerUpdates([
      { chunk_id: 1, speaker: 'Speaker 2' },
      { chunk_id: 2, speaker: 'Speaker 1' },
    ]);
    const segs = store.getState().segments;
    expect(segs.map((s) => [s.speaker, s.timestamp])).toEqual([
      ['Speaker 1', T0],
      ['Speaker 2', T0 + 4_000],
      ['Speaker 1', T0 + 9_000],
    ]);
  });

  it('keeps distinct times across a recording of provisional labels', () => {
    // Labels alternate; each final arrives under the previous final's label,
    // merges, and is split off by its correction a moment later.
    const speakers = ['Speaker 1', 'Speaker 2', 'Speaker 1', 'Speaker 3', 'Speaker 2'];
    let provisional = 'Speaker 1';
    speakers.forEach((real, i) => {
      vi.setSystemTime(T0 + i * 10_000);
      finalArrives(i, `Sentence ${i}.`, provisional);
      store.getState().applySpeakerUpdates([{ chunk_id: i, speaker: real }]);
      provisional = real;
    });
    const segs = store.getState().segments;
    expect(segs.map((s) => s.speaker)).toEqual(speakers);
    expect(segs.map((s) => s.timestamp)).toEqual(speakers.map((_, i) => T0 + i * 10_000));
  });

  it('gives the turns cut from one sentence that sentence time', () => {
    finalArrives(0, 'Hello all.', 'Speaker 1');
    vi.setSystemTime(T0 + 6_000);
    finalArrives(1, 'Right, my point. But wait.', 'Speaker 1');
    vi.setSystemTime(T0 + 8_000);
    store.getState().splitChunk(1, [
      { chunk_id: 1, text: 'Right, my point.', speaker: 'Speaker 1' },
      { chunk_id: 2, text: 'But wait.', speaker: 'Speaker 2' },
    ]);
    const segs = store.getState().segments;
    expect(segs.map((s) => [s.speaker, s.timestamp])).toEqual([
      ['Speaker 1', T0],
      ['Speaker 2', T0 + 6_000],
    ]);
  });

  it('falls back to the segment time for a chunk with no arrival time', () => {
    store.getState().addSegment({
      id: 'seg', speaker: 'Speaker 1', text: 'One. Two.', timestamp: T0, edited: false,
      chunks: [{ id: 0, start: 0 }, { id: 1, start: 5 }],
    });
    store.getState().applySpeakerUpdates([{ chunk_id: 1, speaker: 'Speaker 2' }]);
    expect(store.getState().segments.map((s) => s.timestamp)).toEqual([T0, T0]);
  });

  it('saves the times without the live-only chunk records', () => {
    finalArrives(0, 'Good morning.', 'Speaker 1');
    vi.setSystemTime(T0 + 4_000);
    finalArrives(1, 'Morning.', 'Speaker 1');
    store.getState().applySpeakerUpdates([{ chunk_id: 1, speaker: 'Speaker 2' }]);
    const saved = persistedSegments(store.getState().segments);
    expect(saved.map((s) => s.timestamp)).toEqual([T0, T0 + 4_000]);
    expect(saved.every((s) => s.chunks === undefined)).toBe(true);
  });
});
