import { describe, expect, it, beforeEach, vi } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// settingsStore imports the api client; stub so rendering is inert.
vi.mock('@/api/client', () => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));

import { ResponseLengthPicker } from './ResponseLengthPicker';
import { useSettingsStore } from '@/stores/settingsStore';

const setVerbosity = (v: string) => useSettingsStore.setState({ verbosity: v });

describe('ResponseLengthPicker', () => {
  beforeEach(() => setVerbosity('medium'));

  it('labels the stored verbosity as Brief / Normal / Detailed', () => {
    setVerbosity('low');
    const { rerender } = render(<ResponseLengthPicker />);
    expect(screen.getByRole('button', { name: /Response length: Brief/i })).toBeTruthy();

    setVerbosity('medium');
    rerender(<ResponseLengthPicker />);
    expect(screen.getByRole('button', { name: /Response length: Normal/i })).toBeTruthy();

    setVerbosity('high');
    rerender(<ResponseLengthPicker />);
    expect(screen.getByRole('button', { name: /Response length: Detailed/i })).toBeTruthy();
  });

  it('opens to the three length options with per-option help tips', () => {
    render(<ResponseLengthPicker />);
    fireEvent.click(screen.getByRole('button', { name: /Response length/i }));
    expect(screen.getByTestId('verbosity-option-low').textContent).toContain('Brief');
    expect(screen.getByTestId('verbosity-option-medium').textContent).toContain('Normal');
    expect(screen.getByTestId('verbosity-option-high').textContent).toContain('Detailed');
    // Descriptions live behind the "?" tips, not in the rows.
    expect(screen.getByText('Short, direct answers.')).toBeTruthy();
    expect(screen.getByText('Thorough, expansive answers.')).toBeTruthy();
  });

  it('selecting Brief writes verbosity=low and closes the popover', () => {
    const { container } = render(<ResponseLengthPicker />);
    fireEvent.click(screen.getByRole('button', { name: /Response length/i }));
    fireEvent.click(screen.getByTestId('verbosity-option-low'));
    expect(useSettingsStore.getState().verbosity).toBe('low');
    expect((container.querySelector('.verbosity-pop') as HTMLElement).style.display).toBe('none');
  });

  it('clicking the row area outside the label also selects and closes', () => {
    const { container } = render(<ResponseLengthPicker />);
    fireEvent.click(screen.getByRole('button', { name: /Response length/i }));
    fireEvent.click(screen.getByTestId('verbosity-option-high').closest('.opt-row')!);
    expect(useSettingsStore.getState().verbosity).toBe('high');
    expect((container.querySelector('.verbosity-pop') as HTMLElement).style.display).toBe('none');
  });

  it('renders the pill as label only (no icon, no dot gauge)', () => {
    render(<ResponseLengthPicker />);
    const pill = screen.getByRole('button', { name: /Response length: Normal/i });
    expect(pill.textContent).toBe('Normal');
    expect(pill.querySelector('svg')).toBeNull();
    expect(pill.querySelector('.eg-dot')).toBeNull();
  });
});
