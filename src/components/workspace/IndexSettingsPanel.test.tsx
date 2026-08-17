import { describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { IndexEngineOption, IndexSettings } from '@/api/workspace';

const get = vi.fn();
vi.mock('@/api/client', () => ({
  get: (...args: unknown[]) => get(...args),
  put: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
}));

import { IndexSettingsPanel } from './IndexSettingsPanel';

const HAIKU: IndexEngineOption = {
  key: 'haiku', label: 'Amazon Bedrock (Haiku)', local: false, downloaded: true,
};
const QWEN: IndexEngineOption = {
  key: 'local_qwen35_9b', label: 'Qwen3.5 9B (Local)', local: true, downloaded: true,
};

const settings = (engine: string): IndexSettings => ({
  schedule: { enabled: false, hour: 9, frequency: 'daily', interval_days: 2, weekday: 'mon' },
  typed_relations: { enabled: true, engine },
  entity_descriptions: { enabled: false, engine: 'none' },
  chunk_context: { mode: 'filename', engine: 'local' },
  ner_model: 'gliner',
  refresh_when_closed: false,
});

/** Renders the panel with the catalog the backend would return for a mode. */
async function renderPanel(engines: IndexEngineOption[], engine = 'none') {
  get.mockResolvedValue({ engines, mode: engines.some((e) => !e.local) ? 'hybrid' : 'local' });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const view = render(
    <QueryClientProvider client={qc}>
      <IndexSettingsPanel
        settings={settings(engine)}
        agentSupported
        onChange={vi.fn()}
        onEngineChange={vi.fn()}
        onDescriptionsEngineChange={vi.fn()}
      />
    </QueryClientProvider>,
  );
  await waitFor(() => expect(get).toHaveBeenCalled());
  return view;
}

/** The relationship-engine picker (the only select labelled "Build it using"). */
const enginePicker = () =>
  screen.getByText(/Build it using/).querySelector('select') as HTMLSelectElement;

const optionText = () => Array.from(enginePicker().options).map((o) => o.textContent);

describe('IndexSettingsPanel engine picker', () => {
  it('lists Haiku alongside the on-device models in cloud/hybrid mode', async () => {
    await renderPanel([HAIKU, QWEN]);
    await waitFor(() => expect(optionText()).toContain('Amazon Bedrock (Haiku)'));
    expect(optionText()).toContain('Qwen3.5 9B (Local)');
  });

  it('offers only downloaded on-device models in local mode', async () => {
    await renderPanel([QWEN]);
    await waitFor(() => expect(optionText()).toContain('Qwen3.5 9B (Local)'));
    expect(optionText().join(' ')).not.toMatch(/Haiku|Bedrock/);
  });

  it('never pre-picks a model: the empty sentinel stays selected', async () => {
    await renderPanel([QWEN]);
    await waitFor(() => expect(optionText()).toContain('Qwen3.5 9B (Local)'));
    expect(enginePicker().value).toBe('none');
  });

  it('shows no model at all, plus the download note, when none are installed', async () => {
    await renderPanel([]);
    await waitFor(() =>
      expect(screen.getByText(/Download one in Settings > Models > Chat/)).toBeTruthy(),
    );
    // GLiNER2 needs no chat model, so it stays; nothing else is invented.
    expect(optionText()).toEqual([
      'Choose a model…',
      'GLiNER2 native, local and fast (fewer links)',
    ]);
  });

  it('flags a stored engine the current mode cannot run instead of showing another model', async () => {
    await renderPanel([QWEN], 'haiku');
    await waitFor(() => expect(optionText()).toContain('Not available in this model mode'));
    expect(enginePicker().value).toBe('__unavailable');
  });

  it('renders no engine options at all while the catalog is still loading', async () => {
    get.mockReturnValue(new Promise(() => {}));
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <IndexSettingsPanel
          settings={settings('none')}
          agentSupported
          onChange={vi.fn()}
          onEngineChange={vi.fn()}
          onDescriptionsEngineChange={vi.fn()}
        />
      </QueryClientProvider>,
    );
    expect(optionText().join(' ')).not.toMatch(/Gemma|Qwen|Haiku/);
  });
});
