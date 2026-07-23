import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { ModelDropdown } from './ModelDropdown';

const MODELS = [
  { key: 'opus4.8', name: 'Opus 4.8' },
  { key: 'local_gemma', name: 'Gemma', is_local: true },
];

function renderDropdown(props: Partial<React.ComponentProps<typeof ModelDropdown>> = {}) {
  return render(
    <ModelDropdown
      models={MODELS}
      selectedModel="local_gemma"
      loadedLocalModel={null}
      open={false}
      onToggle={vi.fn()}
      onSelect={vi.fn()}
      {...props}
    />,
  );
}

describe('ModelDropdown — on-device load state', () => {
  it('flags the selected on-device model as not loaded until it is resident', () => {
    renderDropdown({ selectedModel: 'local_gemma', loadedLocalModel: null });
    expect(screen.getByText(/not loaded/i)).toBeTruthy();
  });

  it('drops the "not loaded" flag once the selected on-device model is resident', () => {
    renderDropdown({ selectedModel: 'local_gemma', loadedLocalModel: 'local_gemma' });
    expect(screen.queryByText(/not loaded/i)).toBeNull();
  });

  it('never flags a cloud model as not loaded', () => {
    renderDropdown({ selectedModel: 'opus4.8', loadedLocalModel: null });
    expect(screen.queryByText(/not loaded/i)).toBeNull();
  });

  it('marks the resident on-device model with a loaded dot in the list', () => {
    renderDropdown({ selectedModel: 'local_gemma', loadedLocalModel: 'local_gemma', open: true });
    expect(document.querySelector('[title="Loaded in memory"]')).toBeTruthy();
  });

  it('shows no loaded dot when no on-device model is resident', () => {
    renderDropdown({ selectedModel: 'local_gemma', loadedLocalModel: null, open: true });
    expect(document.querySelector('[title="Loaded in memory"]')).toBeNull();
  });

  it('gives each model option a stable testid keyed by its key, not its position', () => {
    renderDropdown({ open: true });
    expect(screen.getByTestId('model-option-opus4.8')).toBeTruthy();
    expect(screen.getByTestId('model-option-local_gemma')).toBeTruthy();
  });
});
