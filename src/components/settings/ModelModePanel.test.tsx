import { describe, expect, it, beforeEach, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

// settingsStore actions PUT via the api client; stub it so rendering is inert.
vi.mock('@/api/client', () => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));

import { ModelModePanel } from './ModelModePanel';
import { useSettingsStore } from '@/stores/settingsStore';

function setMode(mode: 'cloud' | 'hybrid' | 'local') {
  useSettingsStore.setState((s) => ({ config: { ...s.config, modelMode: mode, backends: {} } }));
}

describe('ModelModePanel', () => {
  beforeEach(() => setMode('cloud'));

  it('shows the mode select but no capability pickers in cloud mode', () => {
    setMode('cloud');
    render(<ModelModePanel />);
    expect((screen.getByLabelText('Mode') as HTMLSelectElement).value).toBe('cloud');
    expect(screen.queryByLabelText('Embeddings (search index)')).toBeNull();
  });

  it('reveals the four per-capability pickers in hybrid mode', () => {
    setMode('hybrid');
    render(<ModelModePanel />);
    expect(screen.getByLabelText('Embeddings (search index)')).toBeTruthy();
    expect(screen.getByLabelText('Reranker')).toBeTruthy();
    expect(screen.getByLabelText('Entity extraction')).toBeTruthy();
    expect(screen.getByLabelText('Index writer (relations, headers)')).toBeTruthy();
    // Unset capabilities default to the cloud backend.
    expect((screen.getByLabelText('Embeddings (search index)') as HTMLSelectElement).value).toBe('cohere');
  });

  it('hides the capability pickers in local mode', () => {
    setMode('local');
    render(<ModelModePanel />);
    expect(screen.queryByLabelText('Embeddings (search index)')).toBeNull();
  });
});
