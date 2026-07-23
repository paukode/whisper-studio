import { render, screen } from '@testing-library/react';
import { describe, it, expect, vi } from 'vitest';
import { ThemeProvider } from '@/providers/ThemeProvider';
import { StardustEffortSlider } from './StardustEffortSlider';

const LEVELS = ['low', 'medium', 'high', 'extra', 'max', 'ultracode'];

function setup(value = 'high', onChange = vi.fn()) {
  render(
    <ThemeProvider>
      <StardustEffortSlider levels={LEVELS} value={value} onChange={onChange} />
    </ThemeProvider>,
  );
  return onChange;
}

describe('StardustEffortSlider', () => {
  it('renders a slider with aria reflecting the current tier', () => {
    setup('high');
    const s = screen.getByRole('slider', { name: /effort/i });
    expect(s).toBeTruthy();
    expect(s.getAttribute('aria-valuemin')).toBe('0');
    expect(s.getAttribute('aria-valuemax')).toBe('5');
    expect(s.getAttribute('aria-valuenow')).toBe('2');
    expect(s.getAttribute('aria-valuetext')).toBe('High');
  });

  it('reflects ultracode as the top index and mounts without a 2d context (jsdom)', () => {
    expect(() => setup('ultracode')).not.toThrow();
    expect(screen.getByRole('slider').getAttribute('aria-valuenow')).toBe('5');
  });
});
