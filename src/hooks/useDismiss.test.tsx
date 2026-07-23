import { describe, it, expect, vi } from 'vitest';
import { render, fireEvent } from '@testing-library/react';
import { useRef } from 'react';
import { useDismiss, useFocusTrap } from './useDismiss';

function DismissHarness({ onDismiss, enabled = true, escape = true, outsideClick = true }: {
  onDismiss: () => void; enabled?: boolean; escape?: boolean; outsideClick?: boolean;
}) {
  const ref = useRef<HTMLDivElement>(null);
  useDismiss(ref, onDismiss, { enabled, escape, outsideClick });
  return (
    <div>
      <button data-testid="outside">outside</button>
      <div ref={ref} data-testid="popup"><button data-testid="inside">inside</button></div>
    </div>
  );
}

describe('useDismiss', () => {
  it('dismisses on Escape', () => {
    const onDismiss = vi.fn();
    render(<DismissHarness onDismiss={onDismiss} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('dismisses on outside mousedown but not inside', () => {
    const onDismiss = vi.fn();
    const { getByTestId } = render(<DismissHarness onDismiss={onDismiss} />);
    fireEvent.mouseDown(getByTestId('inside'));
    expect(onDismiss).not.toHaveBeenCalled();
    fireEvent.mouseDown(getByTestId('outside'));
    expect(onDismiss).toHaveBeenCalledTimes(1);
  });

  it('respects escape:false / outsideClick:false', () => {
    const onDismiss = vi.fn();
    const { getByTestId } = render(
      <DismissHarness onDismiss={onDismiss} escape={false} outsideClick={false} />,
    );
    fireEvent.keyDown(document, { key: 'Escape' });
    fireEvent.mouseDown(getByTestId('outside'));
    expect(onDismiss).not.toHaveBeenCalled();
  });

  it('does nothing when disabled', () => {
    const onDismiss = vi.fn();
    render(<DismissHarness onDismiss={onDismiss} enabled={false} />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onDismiss).not.toHaveBeenCalled();
  });
});

function TrapHarness() {
  const ref = useRef<HTMLDivElement>(null);
  useFocusTrap(ref, true);
  return (
    <div ref={ref}>
      <button data-testid="a">a</button>
      <button data-testid="b">b</button>
    </div>
  );
}

describe('useFocusTrap', () => {
  it('focuses the first focusable on enable', () => {
    const { getByTestId } = render(<TrapHarness />);
    expect(document.activeElement).toBe(getByTestId('a'));
  });

  it('wraps Tab from last back to first', () => {
    const { getByTestId } = render(<TrapHarness />);
    getByTestId('b').focus();
    fireEvent.keyDown(getByTestId('b'), { key: 'Tab' });
    expect(document.activeElement).toBe(getByTestId('a'));
  });
});
