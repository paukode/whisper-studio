import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// The section talks through the shared api client; stub it so every request is
// programmable (same approach as ConfigEditorDialog.test.tsx).
const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  put: vi.fn(),
}));
vi.mock('@/api/client', () => api);

import { ChatModelVisibility } from './ChatModelVisibility';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

const ROWS = {
  disabled: ['haiku'],
  models: [
    { key: 'opus4.8', label: 'Opus 4.8', is_local: false, disabled: false },
    { key: 'haiku', label: 'Haiku 4.5', is_local: false, disabled: true },
    { key: 'local_gemma', label: 'Gemma 4 12B (Local)', is_local: true, disabled: false },
  ],
};

/** GET /api/models payload the settings store refresh consumes. */
const MODELS = {
  models: [
    { key: 'opus4.8', name: 'Opus 4.8' },
    { key: 'local_gemma', name: 'Gemma 4 12B (Local)', is_local: true },
  ],
  default: 'opus4.8',
};

function renderSection() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ChatModelVisibility />
    </QueryClientProvider>,
  );
  return qc;
}

beforeEach(() => {
  vi.clearAllMocks();
  localStorage.clear();
  api.get.mockImplementation((url: string) =>
    Promise.resolve(url === '/api/models' ? MODELS : ROWS),
  );
  useUIStore.setState({ toasts: [] });
  useSettingsStore.setState({ models: [], selectedModel: 'opus4.8' });
});

describe('ChatModelVisibility', () => {
  it('lists every catalog model, including hidden ones, with badges', async () => {
    renderSection();
    expect(await screen.findByText('Opus 4.8')).toBeInTheDocument();
    expect(screen.getByText('Haiku 4.5')).toBeInTheDocument();
    expect(screen.getByText('Gemma 4 12B (Local)')).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/config/models-disabled');
    // The disabled model stays listed (else it could never be re-enabled),
    // flagged with the subtle hidden state and an unchecked toggle.
    expect(screen.getByText('Hidden from picker')).toBeInTheDocument();
    expect(screen.getByRole('checkbox', { name: 'Toggle Haiku 4.5 in the model picker' })).not.toBeChecked();
    expect(screen.getByRole('checkbox', { name: 'Toggle Opus 4.8 in the model picker' })).toBeChecked();
    // On-device models carry the Local badge.
    expect(screen.getByText('Local')).toBeInTheDocument();
  });

  it('toggling a model off PUTs the new hide-list and refreshes the picker store', async () => {
    api.put.mockResolvedValue({
      ok: true,
      disabled: ['haiku', 'sonnet'],
      models: ROWS.models,
    });
    renderSection();
    await screen.findByText('Opus 4.8');

    fireEvent.click(screen.getByRole('checkbox', { name: 'Toggle Opus 4.8 in the model picker' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/config/models-disabled', {
        disabled: ['haiku', 'opus4.8'],
      }),
    );
    // Feature 1: the composer picker store re-fetches /api/models so the model
    // disappears live, with no reload.
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/models', expect.anything()));
    expect(useSettingsStore.getState().models.map((m) => m.key)).toEqual([
      'opus4.8',
      'local_gemma',
    ]);
  });

  it('toggling a hidden model back on removes it from the hide-list', async () => {
    api.put.mockResolvedValue({
      ok: true,
      disabled: [],
      models: ROWS.models.map((m) => ({ ...m, disabled: false })),
    });
    renderSection();
    await screen.findByText('Haiku 4.5');

    fireEvent.click(screen.getByRole('checkbox', { name: 'Toggle Haiku 4.5 in the model picker' }));

    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/config/models-disabled', { disabled: [] }),
    );
    // The response drives the render: the hidden badge clears.
    await waitFor(() => expect(screen.queryByText('Hidden from picker')).not.toBeInTheDocument());
  });

  it('warns when hiding the currently selected model', async () => {
    useSettingsStore.setState({ selectedModel: 'opus4.8' });
    api.put.mockResolvedValue({ ok: true, disabled: ['haiku', 'opus4.8'], models: ROWS.models });
    renderSection();
    await screen.findByText('Opus 4.8');

    fireEvent.click(screen.getByRole('checkbox', { name: 'Toggle Opus 4.8 in the model picker' }));

    await waitFor(() =>
      expect(useUIStore.getState().toasts.map((t) => t.message)).toEqual(
        expect.arrayContaining([expect.stringContaining('Opus 4.8 was your selected chat model')]),
      ),
    );
  });

  it('does not warn when hiding a model that is not selected', async () => {
    useSettingsStore.setState({ selectedModel: 'opus4.8' });
    api.put.mockResolvedValue({ ok: true, disabled: ['haiku', 'local_gemma'], models: ROWS.models });
    renderSection();
    await screen.findByText('Gemma 4 12B (Local)');

    fireEvent.click(
      screen.getByRole('checkbox', { name: 'Toggle Gemma 4 12B (Local) in the model picker' }),
    );

    await waitFor(() => expect(api.put).toHaveBeenCalled());
    expect(useUIStore.getState().toasts).toHaveLength(0);
  });

  it('surfaces a toast when the save fails and leaves the list unchanged', async () => {
    api.put.mockRejectedValue(new Error('disk full'));
    renderSection();
    await screen.findByText('Opus 4.8');

    fireEvent.click(screen.getByRole('checkbox', { name: 'Toggle Opus 4.8 in the model picker' }));

    await waitFor(() =>
      expect(useUIStore.getState().toasts.map((t) => t.message)).toEqual(
        expect.arrayContaining([expect.stringContaining('Could not update Opus 4.8')]),
      ),
    );
    // Still rendered from the last good server state: Opus stays checked.
    expect(screen.getByRole('checkbox', { name: 'Toggle Opus 4.8 in the model picker' })).toBeChecked();
  });
});
