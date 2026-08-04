import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ImportSkillsDialog } from './ImportSkillsDialog';

vi.mock('@/api/client', () => ({
  post: vi.fn(() => Promise.resolve({ skills: [] })),
}));

function renderDialog(props: Partial<React.ComponentProps<typeof ImportSkillsDialog>> = {}) {
  const onClose = vi.fn();
  const onImported = vi.fn();
  render(<ImportSkillsDialog onClose={onClose} onImported={onImported} {...props} />);
  return { onClose, onImported };
}

describe('ImportSkillsDialog — backdrop dismissal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('closes on a genuine backdrop press (mousedown + click on the overlay)', () => {
    const { onClose } = renderDialog();
    const overlay = screen.getByRole('dialog');

    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does NOT close when a selection starts inside the content and releases on the backdrop', () => {
    const { onClose } = renderDialog();
    const overlay = screen.getByRole('dialog');
    const title = screen.getByText('Import skills from Git');

    // Press begins on the content; the resulting click targets the overlay.
    fireEvent.mouseDown(title);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on the × button', () => {
    const { onClose } = renderDialog();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});
