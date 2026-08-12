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

  it('marks an on-device model that has no tool loop as chat only', () => {
    renderDropdown({
      open: true,
      models: [
        { key: 'opus4.8', name: 'Opus 4.8' },
        { key: 'local_tiny', name: 'Qwen2.5-Coder 1.5B (Local)', is_local: true, supports_tools: false },
        { key: 'local_gemma', name: 'Gemma (Local)', is_local: true, supports_tools: true },
      ],
    });
    expect(screen.getAllByText(/chat only/i)).toHaveLength(1);
  });

  it('never marks a cloud model chat-only, whatever its tool flag says', () => {
    renderDropdown({
      open: true,
      models: [{ key: 'opus4.8', name: 'Opus 4.8', supports_tools: false }],
    });
    expect(screen.queryByText(/chat only/i)).toBeNull();
  });
});
