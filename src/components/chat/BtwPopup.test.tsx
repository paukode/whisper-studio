import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { BtwPopup } from './BtwPopup';

function renderPopup(props: Partial<React.ComponentProps<typeof BtwPopup>> = {}) {
  const onClose = vi.fn();
  const { container } = render(
    <BtwPopup question="why?" answer="because" onClose={onClose} {...props} />,
  );
  return { onClose, container };
}

describe('BtwPopup — backdrop dismissal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('closes on a genuine backdrop press (mousedown + click on the overlay)', () => {
    const { onClose, container } = renderPopup();
    const overlay = container.querySelector('.btw-popup-overlay') as HTMLElement;
    expect(overlay).toBeTruthy();

    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does NOT close when a selection starts on the answer and releases on the backdrop', () => {
    const { onClose, container } = renderPopup();
    const overlay = container.querySelector('.btw-popup-overlay') as HTMLElement;
    const answer = screen.getByText('because');

    // Press begins on the answer text; the resulting click targets the overlay —
    // the "select text, release outside" dismissal bug.
    fireEvent.mouseDown(answer);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on the × button', () => {
    const { onClose } = renderPopup();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
