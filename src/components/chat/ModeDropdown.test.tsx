import { describe, expect, it, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ModeDropdown } from './ModeDropdown';

function renderDropdown(props: Partial<React.ComponentProps<typeof ModeDropdown>> = {}) {
  return render(
    <ModeDropdown
      permissionMode="default"
      open={true}
      onToggle={vi.fn()}
      onSelect={vi.fn()}
      onManage={vi.fn()}
      {...props}
    />,
  );
}

// Regression coverage: the dropdown mixes a "Manage rules…" link and a plain
// header div in among the .toolbar-dropdown-item rows, so a positional
// selector (nth-of-type) picks the wrong mode. Each option needs its own
// stable, value-keyed hook.
describe('ModeDropdown — stable option hooks', () => {
  it('gives every mode option a testid keyed by its value', () => {
    renderDropdown();
    for (const value of ['default', 'auto', 'plan', 'acceptEdits', 'bypassPermissions', 'dontAsk']) {
      expect(screen.getByTestId(`mode-option-${value}`)).toBeTruthy();
    }
  });

  it('selecting the Plan option calls onSelect with "plan", not a sibling mode', () => {
    const onSelect = vi.fn();
    renderDropdown({ onSelect });
    fireEvent.click(screen.getByTestId('mode-option-plan'));
    expect(onSelect).toHaveBeenCalledWith('plan');
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it('gives the manage-rules link its own testid, separate from the mode list', () => {
    const onManage = vi.fn();
    renderDropdown({ onManage });
    fireEvent.click(screen.getByTestId('mode-option-manage'));
    expect(onManage).toHaveBeenCalledTimes(1);
  });
});

describe('ModeDropdown — pill and risk affordances', () => {
  it('shows the friendly label on the pill, not the raw mode value', () => {
    renderDropdown({ permissionMode: 'bypassPermissions' });
    const pill = screen.getByTitle('Permission mode: Bypass');
    expect(pill.textContent).toBe('Bypass');
    expect(pill.className).toContain('mode-danger');
  });

  it('tints the pill amber for acceptEdits and leaves safe modes neutral', () => {
    const { rerender } = renderDropdown({ permissionMode: 'acceptEdits' });
    expect(screen.getByTitle('Permission mode: Accept edits').className).toContain('mode-warn');

    rerender(
      <ModeDropdown permissionMode="auto" open={true} onToggle={vi.fn()} onSelect={vi.fn()} onManage={vi.fn()} />,
    );
    const pill = screen.getByTitle('Permission mode: Auto');
    expect(pill.className).not.toContain('mode-warn');
    expect(pill.className).not.toContain('mode-danger');
  });

  it('marks only the risky rows with a warning icon', () => {
    renderDropdown();
    const row = (v: string) => screen.getByTestId(`mode-option-${v}`).closest('.opt-row')!;
    expect(row('bypassPermissions').querySelector('.mode-risk-danger')).toBeTruthy();
    expect(row('acceptEdits').querySelector('.mode-risk-warn')).toBeTruthy();
    expect(row('plan').querySelector('.mode-risk-icon')).toBeNull();
  });

  it('describes what each mode enforces behind the per-row help tip', () => {
    renderDropdown();
    expect(screen.getByText(/Everything runs immediately\. No prompts/)).toBeTruthy();
    expect(screen.getByText(/File writes and creates run without asking/)).toBeTruthy();
  });

  it('clicking a help tip does not select the mode', () => {
    const onSelect = vi.fn();
    renderDropdown({ onSelect });
    fireEvent.click(screen.getByText(/changes come back as a proposal/));
    expect(onSelect).not.toHaveBeenCalled();
  });

  it('selects when clicking anywhere on the row, not just the label button', () => {
    const onSelect = vi.fn();
    renderDropdown({ onSelect });
    fireEvent.click(screen.getByTestId('mode-option-plan').closest('.opt-row')!);
    expect(onSelect).toHaveBeenCalledWith('plan');
    expect(onSelect).toHaveBeenCalledTimes(1);
  });

  it('renders mode options as buttons with pressed state on the current mode', () => {
    renderDropdown({ permissionMode: 'auto' });
    const auto = screen.getByTestId('mode-option-auto');
    expect(auto.tagName).toBe('BUTTON');
    expect(auto.getAttribute('aria-pressed')).toBe('true');
    expect(screen.getByTestId('mode-option-plan').getAttribute('aria-pressed')).toBe('false');
  });

  it('exposes expanded state on the pill', () => {
    renderDropdown({ permissionMode: 'auto' });
    expect(screen.getByTitle('Permission mode: Auto').getAttribute('aria-expanded')).toBe('true');
  });
});
