import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { HelpTip } from './HelpTip';

describe('HelpTip', () => {
  it('is a real button described by its tooltip text', () => {
    render(<HelpTip text="Balanced detail." />);
    const btn = screen.getByRole('button', { name: 'Help' });
    const tip = screen.getByText('Balanced detail.');
    expect(tip.getAttribute('role')).toBe('tooltip');
    expect(btn.getAttribute('aria-describedby')).toBe(tip.id);
  });

  it('swallows clicks so a tip inside a selectable row never triggers it', () => {
    const onRowClick = vi.fn();
    render(
      <div onClick={onRowClick}>
        <HelpTip text="Short, direct answers." />
      </div>,
    );
    fireEvent.click(screen.getByRole('button', { name: 'Help' }));
    expect(onRowClick).not.toHaveBeenCalled();
  });

  it('dismisses on Escape by dropping focus, without bubbling to global ESC handlers', () => {
    render(<HelpTip text="Thorough, expansive answers." />);
    const btn = screen.getByRole('button', { name: 'Help' });
    btn.focus();
    expect(document.activeElement).toBe(btn);
    fireEvent.keyDown(btn, { key: 'Escape' });
    expect(document.activeElement).not.toBe(btn);
  });
});
