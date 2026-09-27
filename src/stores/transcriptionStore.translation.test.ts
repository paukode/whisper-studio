import { beforeEach, describe, expect, it } from 'vitest';
import { createTranscriptionStore } from './transcriptionStore';

describe('translate-to-English companion lines', () => {
  let testStore: ReturnType<typeof createTranscriptionStore>;
  beforeEach(() => {
    testStore = createTranscriptionStore();
  });

  const addPolishSegment = () => {
    testStore.getState().addSegment({
      id: 'seg-1', speaker: 'Speaker 1', text: 'Dzień dobry',
      timestamp: 1, edited: false,
      chunks: [{ id: 0, start: 0 }],
      pendingTranslations: [0],
    });
  };

  it('applyTranslation resolves a pending chunk into a translation line', () => {
    addPolishSegment();
    testStore.getState().applyTranslation(0, 'Good morning');
    const seg = testStore.getState().segments[0];
    expect(seg.translations).toEqual([{ chunkId: 0, text: 'Good morning' }]);
    expect(seg.pendingTranslations).toBeUndefined();
  });

  it('empty translation clears pending without adding a line', () => {
    addPolishSegment();
    testStore.getState().applyTranslation(0, '');
    const seg = testStore.getState().segments[0];
    expect(seg.translations).toBeUndefined();
    expect(seg.pendingTranslations).toBeUndefined();
  });

  it('appendSegmentText tracks pending per merged chunk, translations stay in chunk order', () => {
    addPolishSegment();
    const store = testStore.getState();
    store.appendSegmentText('seg-1', 'wszystkim', 1, true);
    expect(testStore.getState().segments[0].pendingTranslations).toEqual([0, 1]);
    // Out-of-order arrival still renders in chunk order.
    store.applyTranslation(1, 'everyone');
    store.applyTranslation(0, 'Good morning');
    const seg = testStore.getState().segments[0];
    expect(seg.translations).toEqual([
      { chunkId: 0, text: 'Good morning' },
      { chunkId: 1, text: 'everyone' },
    ]);
  });

  it('speaker split carries each translation with its source chunk', () => {
    addPolishSegment();
    const store = testStore.getState();
    store.appendSegmentText('seg-1', 'General Kenobi', 1, true);
    store.applyTranslation(0, 'Good morning');
    store.applyTranslation(1, 'General Kenobi EN');
    store.applySpeakerUpdates([{ chunk_id: 1, speaker: 'Speaker 2' }]);
    const segs = testStore.getState().segments;
    expect(segs).toHaveLength(2);
    expect(segs[0].translations).toEqual([{ chunkId: 0, text: 'Good morning' }]);
    expect(segs[1].translations).toEqual([{ chunkId: 1, text: 'General Kenobi EN' }]);
  });

  it('loadSegments strips stale pending markers but keeps translations', () => {
    testStore.getState().loadSegments(
      [{
        id: 'h-1', speaker: 'Speaker 1', text: 'Cześć', timestamp: 1, edited: false,
        translations: [{ chunkId: 3, text: 'Hi' }], pendingTranslations: [4],
      }],
      {},
    );
    const seg = testStore.getState().segments[0];
    expect(seg.translations).toEqual([{ chunkId: 3, text: 'Hi' }]);
    expect(seg.pendingTranslations).toBeUndefined();
  });

  it('a loaded transcript cannot adopt a live chunk that reuses its old ids', () => {
    testStore.getState().loadSegments(
      [{
        id: 'h-1', speaker: 'Speaker 1', text: 'Cześć', timestamp: 1, edited: false,
        chunks: [{ id: 3, start: 0 }], translations: [{ chunkId: 3, text: 'Hi' }],
      }],
      {},
    );
    const store = testStore.getState();
    store.applyTranslation(3, 'Somebody else');
    store.applySpeakerUpdates([{ chunk_id: 3, speaker: 'Speaker 2' }]);
    const seg = testStore.getState().segments[0];
    expect(seg.translations).toEqual([{ chunkId: 3, text: 'Hi' }]);
    expect(seg.speaker).toBe('Speaker 1');
  });
});

describe('segment re-translation after a manual edit', () => {
  let testStore: ReturnType<typeof createTranscriptionStore>;
  beforeEach(() => {
    testStore = createTranscriptionStore();
    testStore.getState().addSegment({
      id: 'seg-1', speaker: 'Speaker 1', text: 'Dzień dobry',
      timestamp: 1, edited: false,
      chunks: [{ id: 0, start: 0 }],
      translations: [{ chunkId: 0, text: 'Good morning', target: 'en' }],
    });
  });

  it('begin parks the pending marker and drops the stale lines', () => {
    testStore.getState().beginSegmentRetranslation('seg-1');
    const seg = testStore.getState().segments[0];
    expect(seg.translations).toBeUndefined();
    expect(seg.pendingTranslations).toEqual([-1]);
  });

  it('complete commits one fresh line; empty text clears everything', () => {
    const store = testStore.getState();
    store.beginSegmentRetranslation('seg-1');
    store.completeSegmentRetranslation('seg-1', 'Good evening', 'en');
    let seg = testStore.getState().segments[0];
    expect(seg.translations).toEqual([{ chunkId: -1, text: 'Good evening', target: 'en' }]);
    expect(seg.pendingTranslations).toBeUndefined();
    store.beginSegmentRetranslation('seg-1');
    store.completeSegmentRetranslation('seg-1', '', 'en');
    seg = testStore.getState().segments[0];
    expect(seg.translations).toBeUndefined();
  });

  it('late machine chunk translations cannot overwrite an in-flight edit', () => {
    const store = testStore.getState();
    store.beginSegmentRetranslation('seg-1');
    store.applyTranslation(0, 'stale machine line', 'en');
    const seg = testStore.getState().segments[0];
    expect(seg.translations).toBeUndefined();
    expect(seg.pendingTranslations).toEqual([-1]);
  });
});

describe('an edit supersedes the machine lines of the text it replaced', () => {
  let testStore: ReturnType<typeof createTranscriptionStore>;
  // Chunks 4 and 5 built the segment; chunk 5's decode is still queued.
  beforeEach(() => {
    testStore = createTranscriptionStore();
    testStore.getState().addSegment({
      id: 'seg-1', speaker: 'Speaker 1', text: 'Dzień dobry. Jak się mosz?',
      timestamp: 1, edited: false,
      chunks: [{ id: 4, start: 0 }, { id: 5, start: 13 }],
      translations: [{ chunkId: 4, text: 'Good morning.', target: 'en' }],
      pendingTranslations: [5],
    });
  });

  const lines = () => (testStore.getState().segments[0].translations ?? []).map((t) => t.text);

  it('a decode of the old audio that lands after the re-translation is not appended', () => {
    const store = testStore.getState();
    store.editSegmentText('seg-1', 'Dzień dobry. Jak się masz?');
    store.beginSegmentRetranslation('seg-1');
    store.completeSegmentRetranslation('seg-1', 'Good morning. How are you?', 'en');
    store.applyTranslation(5, 'How are you mosz?', 'en');

    expect(lines()).toEqual(['Good morning. How are you?']);
    expect(testStore.getState().segments[0].pendingTranslations).toBeUndefined();
  });

  it('without a re-translation the stale lines still drop, and a late decode stays out', () => {
    const store = testStore.getState();
    store.editSegmentText('seg-1', 'Dzień dobry. Jak się masz?');
    // Outside the Mac app the panel only drops the stale line.
    store.completeSegmentRetranslation('seg-1', '', 'en');
    store.applyTranslation(5, 'How are you mosz?', 'en');

    expect(testStore.getState().segments[0].translations).toBeUndefined();
    expect(testStore.getState().segments[0].pendingTranslations).toBeUndefined();
  });

  it('a chunk appended after the edit keeps its line, during and after the re-translation', () => {
    const store = testStore.getState();
    store.editSegmentText('seg-1', 'Dzień dobry. Jak się masz?');
    store.beginSegmentRetranslation('seg-1');
    // The same speaker keeps talking: chunk 6 grows the edited segment.
    store.appendSegmentText('seg-1', 'Dobrze.', 6, true);
    store.applyTranslation(6, 'Fine.', 'en');
    expect(lines()).toEqual(['Fine.']);
    expect(testStore.getState().segments[0].pendingTranslations).toEqual([-1]);

    store.completeSegmentRetranslation('seg-1', 'Good morning. How are you?', 'en');
    store.appendSegmentText('seg-1', 'Naprawdę.', 7, true);
    store.applyTranslation(7, 'Really.', 'en');

    // The edit's line covers the edited text; later chunks follow in order.
    expect(lines()).toEqual(['Good morning. How are you?', 'Fine.', 'Really.']);
    expect(testStore.getState().segments[0].pendingTranslations).toBeUndefined();
  });

  it('a speaker split keeps the edit line with the part that keeps the segment id', () => {
    const store = testStore.getState();
    store.editSegmentText('seg-1', 'Dzień dobry. Jak się masz?');
    store.beginSegmentRetranslation('seg-1');
    store.completeSegmentRetranslation('seg-1', 'Good morning. How are you?', 'en');
    store.appendSegmentText('seg-1', 'Dobrze.', 6, true);
    store.applyTranslation(6, 'Fine.', 'en');
    store.applySpeakerUpdates([{ chunk_id: 6, speaker: 'Speaker 2' }]);

    const [first, second] = testStore.getState().segments;
    expect(first.id).toBe('seg-1');
    expect(first.translations?.map((t) => t.text)).toEqual(['Good morning. How are you?']);
    expect(second.speaker).toBe('Speaker 2');
    expect(second.translations?.map((t) => t.text)).toEqual(['Fine.']);
  });
});
