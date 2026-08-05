import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ConfirmDialog } from './ConfirmDialog';

function renderConfirm(overrides: Partial<React.ComponentProps<typeof ConfirmDialog>> = {}) {
  const onConfirm = vi.fn();
  const onCancel = vi.fn();
  render(
    <ConfirmDialog
      title="Trust skill?"
      message="This lets the skill run shell commands."
      onConfirm={onConfirm}
      onCancel={onCancel}
      {...overrides}
    />,
  );
  return { onConfirm, onCancel };
}

describe('ConfirmDialog — backdrop dismissal', () => {
  it('cancels on a genuine backdrop press (mousedown + click on the overlay)', () => {
    const { onCancel } = renderConfirm();
    const overlay = screen.getByRole('dialog');

    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(onCancel).toHaveBeenCalledTimes(1);
  });

  it('does NOT cancel when a selection starts on the message and releases on the backdrop', () => {
    const { onCancel } = renderConfirm();
    const overlay = screen.getByRole('dialog');
    const message = screen.getByText(/lets the skill run/i);

    // Press begins on the content; release (click) targets the overlay.
    fireEvent.mouseDown(message);
    fireEvent.click(overlay);

    expect(onCancel).not.toHaveBeenCalled();
  });

  it('cancels on the Cancel button and confirms on the confirm button', () => {
    const { onConfirm, onCancel } = renderConfirm({ confirmLabel: 'Trust' });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onCancel).toHaveBeenCalledTimes(1);
    fireEvent.click(screen.getByRole('button', { name: 'Trust' }));
    expect(onConfirm).toHaveBeenCalledTimes(1);
  });
});
