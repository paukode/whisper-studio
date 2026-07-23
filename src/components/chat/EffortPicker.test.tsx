import { describe, expect, it, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// settingsStore imports the api client; stub so rendering is inert.
vi.mock('@/api/client', () => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));

import { EffortPicker } from './EffortPicker';
import { useSettingsStore } from '@/stores/settingsStore';

const FULL_TIERS = ['low', 'medium', 'high', 'extra', 'max', 'ultracode'];
// GPT models carry 'none' (server/infrastructure/effort.py EFFORT_TIERS['openai']).
const OPENAI_TIERS = ['none', 'low', 'medium', 'high', 'max', 'ultracode'];

function setModel(effortLevel: string, tiers: string[] = FULL_TIERS) {
  useSettingsStore.setState({
    models: [{ key: 'fable', label: 'Fable 5', effort_levels: tiers }] as never,
    selectedModel: 'fable',
    effortLevel,
  });
}

describe('EffortPicker — popover header', () => {
  beforeEach(() => setModel('ultracode'));

  it('shows the current tier name next to the Effort label', () => {
    render(<EffortPicker />);
    const val = screen.getByTestId('effort-current');
    expect(val.textContent).toContain('Ultracode');
    expect(val.className).toContain('effort-ultracode');
  });

  it('keeps the tier description behind the help tip, with no tick row or note', () => {
    const { container } = render(<EffortPicker />);
    expect(screen.getByText(/parallel subagents\. Slowest overall; highest quality and cost/)).toBeTruthy();
    expect(container.querySelector('.effort-pop-tick')).toBeNull();
    expect(container.querySelector('.effort-pop-note')).toBeNull();
  });

  it('tracks the tier on the header when the level changes', () => {
    setModel('low');
    render(<EffortPicker />);
    const val = screen.getByTestId('effort-current');
    expect(val.textContent).toContain('Low');
    expect(screen.getByText(/Light reasoning\. Fast and cheap/)).toBeTruthy();
  });

  it('renders the pill as label only, like the mode and verbosity pills', () => {
    render(<EffortPicker />);
    const pill = screen.getByRole('button', { name: /Effort level: Ultracode/i });
    expect(pill.textContent).toBe('Ultracode');
    expect(pill.querySelector('svg')).toBeNull();
    expect(pill.querySelector('.eg-dot')).toBeNull();
  });

  it('mentions speed and cost in every tier description, including none', () => {
    for (const lv of [...FULL_TIERS, 'none']) {
      setModel(lv, lv === 'none' ? OPENAI_TIERS : FULL_TIERS);
      const { unmount } = render(<EffortPicker />);
      const tip = document.querySelector('.help-tip-bubble')!;
      expect(tip.textContent).toMatch(/fast|slow|speed/i);
      expect(tip.textContent).toMatch(/cheap|cost|pric|expensive/i);
      unmount();
    }
  });
});
