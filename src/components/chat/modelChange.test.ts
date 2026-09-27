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
// The data-retention PUT (cloud Mythos paths): enables retention by default.
const { putMock } = vi.hoisted(() => ({
  putMock: vi.fn(async (): Promise<unknown> => ({ mode: 'provider_data_share', enabled: true })),
}));
vi.mock('@/api/client', () => ({ put: putMock, get: vi.fn() }));

import { requestModelChange } from './dataRetentionConsent';
import { useSettingsStore, type ModelEntry } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

const cloud = (key: string): ModelEntry => ({ key, name: key });
const local = (key: string): ModelEntry => ({ key, name: key, is_local: true });

const gated = (key: string): ModelEntry => ({ key, name: key, requires_data_retention: true });

const MODELS: ModelEntry[] = [
  cloud('opus4.8'),
  gated('fable5.1'),
  local('local_gemma'),
  local('local_coder'),
];

beforeEach(() => {
  putMock.mockClear();
  useUIStore.setState({ dialogStack: [], toasts: [] });
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

/** Answer the consent screen the switch opens (true = confirm, false = decline). */
async function answerConsent(value: boolean): Promise<void> {
  await vi.waitFor(() => expect(useUIStore.getState().dialogStack.length).toBe(1));
  const { id } = useUIStore.getState().dialogStack[0];
  useUIStore.getState().resolveDialog(id, value);
}

describe('requestModelChange: leaving a local model for a retention-gated one', () => {
  beforeEach(() => {
    useSettingsStore.setState({ selectedModel: 'local_gemma', loadedLocalModel: 'local_gemma' });
  });

  it('keeps the local model loaded when the consent screen is declined', async () => {
    const pending = requestModelChange('fable5.1');
    await answerConsent(false);
    expect(await pending).toBe(false);
    expect(unloadLocalModelMock).not.toHaveBeenCalled();
    expect(useSettingsStore.getState().selectedModel).toBe('local_gemma');
  });

  it('keeps the local model loaded when enabling retention fails', async () => {
    putMock.mockRejectedValueOnce(new Error('No AWS credentials'));
    const pending = requestModelChange('fable5.1');
    await answerConsent(true);
    expect(await pending).toBe(false);
    expect(unloadLocalModelMock).not.toHaveBeenCalled();
    expect(useSettingsStore.getState().selectedModel).toBe('local_gemma');
  });

  it('frees the local model once the switch commits', async () => {
    const pending = requestModelChange('fable5.1');
    await answerConsent(true);
    expect(await pending).toBe(true);
    expect(useSettingsStore.getState().selectedModel).toBe('fable5.1');
    expect(unloadLocalModelMock).toHaveBeenCalledTimes(1);
  });
});
