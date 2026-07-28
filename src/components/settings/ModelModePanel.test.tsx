import { describe, expect, it, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// settingsStore actions PUT via the api client; stub it so rendering is inert.
// The engines catalog resolves with one local model so the LLM pickers render
// their dynamic options without a live backend.
vi.mock('@/api/client', () => ({
  get: vi.fn().mockResolvedValue({
    engines: [
      { key: 'haiku', label: 'Claude Haiku', local: false, downloaded: true },
      { key: 'local_qwen35_9b', label: 'Qwen3.5 9B (Local)', local: true, downloaded: true },
    ],
  }),
  put: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
}));

import { ModelModePanel } from './ModelModePanel';
import { useSettingsStore } from '@/stores/settingsStore';

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelModePanel />
    </QueryClientProvider>,
  );
}

function setMode(mode: 'cloud' | 'hybrid' | 'local') {
  useSettingsStore.setState((s) => ({ config: { ...s.config, modelMode: mode, backends: {} } }));
}

describe('ModelModePanel', () => {
  beforeEach(() => setMode('cloud'));

  it('shows the mode select but no capability pickers in cloud mode', () => {
    setMode('cloud');
    renderPanel();
    expect((screen.getByLabelText('Mode') as HTMLSelectElement).value).toBe('cloud');
    expect(screen.queryByLabelText('Embeddings (search index)')).toBeNull();
  });

  it('reveals the four per-capability pickers in hybrid mode', async () => {
    setMode('hybrid');
    renderPanel();
    expect(screen.getByLabelText('Embeddings (search index)')).toBeTruthy();
    expect(screen.getByLabelText('Reranker')).toBeTruthy();
    expect(screen.getByLabelText('Entity extraction')).toBeTruthy();
    expect(screen.getByLabelText('One-shot writer (summaries)')).toBeTruthy();
    // Unset capabilities default to the cloud backend.
    expect((screen.getByLabelText('Embeddings (search index)') as HTMLSelectElement).value).toBe('cohere');
  });

  it('lists every local model from the engines catalog in the LLM pickers', async () => {
    setMode('hybrid');
    renderPanel();
    // The catalog query resolves async; the Qwen option appears in both the
    // entity-extraction and one-shot-writer selects once it lands.
    const options = await screen.findAllByRole('option', { name: 'Qwen3.5 9B (Local)' });
    expect(options.length).toBe(2);
    const ner = screen.getByLabelText('Entity extraction') as HTMLSelectElement;
    const texts = Array.from(ner.options).map((o) => o.text);
    expect(texts).toContain('GLiNER — on-device extractor');
    expect(texts).toContain('Claude Haiku — Bedrock');
  });

  it('hides the capability pickers in local mode', () => {
    setMode('local');
    renderPanel();
    expect(screen.queryByLabelText('Embeddings (search index)')).toBeNull();
  });
});
