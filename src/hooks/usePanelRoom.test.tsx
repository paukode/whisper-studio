import { describe, expect, it } from 'vitest';
import { render } from '@testing-library/react';
import { useRef } from 'react';
import { usePanelRoom } from './usePanelRoom';

function rect(top: number, bottom: number): DOMRect {
  return { top, bottom, left: 0, right: 0, width: 0, height: bottom - top, x: 0, y: top, toJSON: () => ({}) } as DOMRect;
}

function Panel({ open, bottom, clipTop }: { open: boolean; bottom: number; clipTop: number }) {
  const ref = useRef<HTMLDivElement>(null);
  const room = usePanelRoom(ref, open);
  return (
    <div
      data-testid="clip"
      style={{ overflowY: 'hidden' }}
      ref={(el) => { if (el) el.getBoundingClientRect = () => rect(clipTop, 2000); }}
    >
      <div
        data-testid="panel"
        ref={(el) => {
          (ref as React.MutableRefObject<HTMLDivElement | null>).current = el;
          if (el) el.getBoundingClientRect = () => rect(bottom - 300, bottom);
        }}
        data-room={room ?? ''}
      />
    </div>
  );
}

describe('usePanelRoom', () => {
  it('gives an open panel the space between its bottom edge and the top of the box that clips it', () => {
    const { getByTestId } = render(<Panel open bottom={380} clipTop={52} />);
    const room = Number(getByTestId('panel').dataset.room);
    // The panel ends inside the clipping box, with a margin under its top edge.
    expect(room).toBeLessThan(380 - 52);
    expect(room).toBeGreaterThan(380 - 52 - 40);
  });

  it('keeps a usable height when the panel sits near the top', () => {
    const { getByTestId } = render(<Panel open bottom={90} clipTop={52} />);
    expect(Number(getByTestId('panel').dataset.room)).toBeGreaterThanOrEqual(160);
  });

  it('sets no height while the panel is closed', () => {
    const { getByTestId } = render(<Panel open={false} bottom={380} clipTop={52} />);
    expect(getByTestId('panel').dataset.room).toBe('');
  });
});
