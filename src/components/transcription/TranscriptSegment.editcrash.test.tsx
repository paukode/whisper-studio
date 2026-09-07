import { render, fireEvent, cleanup } from '@testing-library/react';
import { afterEach, describe, it, expect } from 'vitest';
import { TranscriptSegment } from './TranscriptSegment';
import type { TranscriptSegment as Seg } from '@/types/session';

afterEach(cleanup);

const seg = (over: Partial<Seg>): Seg => ({
  id: 's1',
  speaker: 'SPEAKER_00',
  text: 'hello world',
  timestamp: 0,
  edited: false,
  ...over,
});

const noop = () => {};

function renderSeg(s: Seg) {
  return render(
    <TranscriptSegment segment={s} speakerName="Alex" onTextEdit={noop} onSpeakerRename={noop} />,
  );
}

describe('TranscriptSegment double-click edit — no JS loop or throw', () => {
  it('a normal block enters edit and accepts typing', () => {
    const { container } = renderSeg(seg({ text: 'a normal sentence to edit' }));
    const text = container.querySelector('.segment-text') as HTMLElement;
    fireEvent.doubleClick(text);
    const ta = container.querySelector('textarea') as HTMLTextAreaElement;
    expect(ta).toBeTruthy();
    fireEvent.change(ta, { target: { value: 'edited text' } });
    expect(ta.value).toBe('edited text');
  });

  it('a very large block (200k chars) enters edit without throwing or hanging', () => {
    const huge = 'word '.repeat(40000); // 200k chars, 40k words
    const { container } = renderSeg(seg({ text: huge }));
    const text = container.querySelector('.segment-text') as HTMLElement;
    // If double-click entering edit triggered an infinite render loop, this
    // call would never return and the test would time out.
    fireEvent.doubleClick(text);
    const ta = container.querySelector('textarea') as HTMLTextAreaElement;
    expect(ta).toBeTruthy();
    expect(ta.value.length).toBe(huge.length);
  });

  it('is memoized so a re-rendering panel does not re-render every segment', () => {
    // The freeze came from re-rendering the whole (unvirtualized) list on every
    // live chunk. memo + stable segment identity + stable callbacks is the fix;
    // this guards it from being unwrapped later.
    expect((TranscriptSegment as unknown as { $$typeof?: symbol }).$$typeof).toBe(
      Symbol.for('react.memo'),
    );
  });

  it('a fresh large block (word-reveal animation path) renders without throwing', () => {
    // receivedAt now + freshIndex 0 forces renderSegmentText into the
    // span-per-word animation path — thousands of spans for a big block.
    const huge = 'word '.repeat(5000);
    const { container } = renderSeg(seg({ text: huge, receivedAt: Date.now(), freshIndex: 0 }));
    expect(container.querySelectorAll('.segment-word-reveal').length).toBeGreaterThan(1000);
  });
});
