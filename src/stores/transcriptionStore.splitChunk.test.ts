/**
 * The server sends each sentence before its speaker is known and, when it
 * finds a handover inside one, sends `transcript_split`: that sentence's
 * chunk becomes one chunk per turn, and the segment splits where the
 * speaker changes. Sentences around it keep their text and labels.
 */
import { beforeEach, describe, expect, it } from 'vitest';
import { createTranscriptionStore } from './transcriptionStore';

describe('splitChunk', () => {
  let store: ReturnType<typeof createTranscriptionStore>;
  beforeEach(() => {
    store = createTranscriptionStore();
    const s = store.getState();
    s.addSegment({
      id: 'seg', speaker: 'Speaker 1', text: 'Hello all.', timestamp: 1, edited: false,
      chunks: [{ id: 0, start: 0 }],
    });
    s.appendSegmentText('seg', 'Right, my point. But wait.', 1);
    s.appendSegmentText('seg', 'Okay then.', 2);
  });

  it('cuts the sentence at the handover and splits the segment there', () => {
    store.getState().splitChunk(1, [
      { chunk_id: 1, text: 'Right, my point.', speaker: 'Speaker 1' },
      { chunk_id: 3, text: 'But wait.', speaker: 'Speaker 2' },
    ]);
    const segs = store.getState().segments;
    expect(segs.map((s) => [s.speaker, s.text])).toEqual([
      ['Speaker 1', 'Hello all. Right, my point.'],
      ['Speaker 2', 'But wait.'],
      ['Speaker 1', 'Okay then.'],
    ]);
    // The segment that keeps the original id is the first part.
    expect(segs[0].id).toBe('seg');
    expect(segs[1].chunks).toMatchObject([{ id: 3, start: 0 }]);
  });

  it('keeps one segment when every turn has the same speaker', () => {
    store.getState().splitChunk(1, [
      { chunk_id: 1, text: 'Right, my point.', speaker: 'Speaker 1' },
      { chunk_id: 3, text: 'But wait.', speaker: 'Speaker 1' },
    ]);
    const [seg] = store.getState().segments;
    expect(seg.text).toBe('Hello all. Right, my point. But wait. Okay then.');
    // Later chunks still start where their text starts.
    for (const c of seg.chunks ?? []) {
      const next = seg.chunks!.find((d) => d.start > c.start);
      const piece = seg.text.slice(c.start, next ? next.start - 1 : undefined);
      expect(['Hello all.', 'Right, my point.', 'But wait.', 'Okay then.']).toContain(piece);
    }
  });

  it('ignores a chunk that is no longer on screen', () => {
    const before = store.getState().segments;
    store.getState().splitChunk(99, [{ chunk_id: 99, text: 'x', speaker: 'Speaker 2' }]);
    expect(store.getState().segments).toEqual(before);
  });
});
