import { describe, it, expect, vi, beforeEach } from 'vitest';

// requestModelChange dynamically imports these; assert the load/unload calls.
const { loadLocalModelMock, unloadLocalModelMock } = vi.hoisted(() => ({
  loadLocalModelMock: vi.fn(async () => true),
  unloadLocalModelMock: vi.fn(async () => {}),
}));
vi.mock('@/api/localModel', () => ({
  loadLocalModel: loadLocalModelMock,
  unloadLocalModel: unloadLocalModelMock,
}));
// Only used on the data-retention (cloud Mythos) paths, not exercised here.
vi.mock('@/api/client', () => ({ put: vi.fn(async () => ({})), get: vi.fn() }));

import { requestModelChange } from './dataRetentionConsent';
import { useSettingsStore, type ModelEntry } from '@/stores/settingsStore';

const cloud = (key: string): ModelEntry => ({ key, name: key });
const local = (key: string): ModelEntry => ({ key, name: key, is_local: true });

const MODELS: ModelEntry[] = [cloud('opus4.8'), local('local_gemma'), local('local_coder')];

beforeEach(() => {
  loadLocalModelMock.mockClear();
  loadLocalModelMock.mockResolvedValue(true);
  unloadLocalModelMock.mockClear();
  useSettingsStore.setState({
    models: MODELS,
    selectedModel: 'local_gemma',
    loadedLocalModel: null,
    dataRetentionEnabled: false,
    localContextWindow: 16384,
  });
});

describe('requestModelChange — lazy on-device load', () => {
  it('loads an unloaded local model even when re-selecting the current selection', async () => {
    // The default selection is no longer eager-loaded at startup, so picking it
    // again is exactly how the user starts a session — it must load.
    const ok = await requestModelChange('local_gemma');
    expect(loadLocalModelMock).toHaveBeenCalledWith('local_gemma', 'local_gemma', 16384);
    expect(ok).toBe(true);
    expect(useSettingsStore.getState().selectedModel).toBe('local_gemma');
  });

  it('is a no-op when re-selecting a local model that is already resident', async () => {
    useSettingsStore.setState({ loadedLocalModel: 'local_gemma' });
    const ok = await requestModelChange('local_gemma');
    expect(loadLocalModelMock).not.toHaveBeenCalled();
    expect(ok).toBe(false);
  });

  it('loads a different on-device model when switching local -> local', async () => {
    useSettingsStore.setState({ loadedLocalModel: 'local_gemma' });
    const ok = await requestModelChange('local_coder');
    expect(loadLocalModelMock).toHaveBeenCalledWith('local_coder', 'local_coder', 16384);
    expect(ok).toBe(true);
  });

  it('aborts (selection unchanged) when the load fails', async () => {
    loadLocalModelMock.mockResolvedValueOnce(false);
    useSettingsStore.setState({ selectedModel: 'opus4.8', loadedLocalModel: null });
    const ok = await requestModelChange('local_gemma');
    expect(ok).toBe(false);
    expect(useSettingsStore.getState().selectedModel).toBe('opus4.8');
  });

  it('frees the resident local model when switching to a cloud model', async () => {
    useSettingsStore.setState({ selectedModel: 'local_gemma', loadedLocalModel: 'local_gemma' });
    const ok = await requestModelChange('opus4.8');
    expect(unloadLocalModelMock).toHaveBeenCalled();
    expect(ok).toBe(true);
    expect(useSettingsStore.getState().selectedModel).toBe('opus4.8');
  });

  it('does nothing when re-selecting the same cloud model', async () => {
    useSettingsStore.setState({ selectedModel: 'opus4.8', loadedLocalModel: null });
    const ok = await requestModelChange('opus4.8');
    expect(ok).toBe(false);
    expect(loadLocalModelMock).not.toHaveBeenCalled();
  });
});
