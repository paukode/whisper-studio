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
