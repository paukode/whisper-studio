import { describe, it, expect, vi, beforeEach } from 'vitest';

// settingsStore.setAutoMemory persists via put() from the api client.
const { putMock, getMock } = vi.hoisted(() => ({ putMock: vi.fn(), getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: putMock }));
const { loadStatusMock } = vi.hoisted(() => ({ loadStatusMock: vi.fn() }));
vi.mock('@/services/voiceController', () => ({ voiceController: { loadStatus: loadStatusMock } }));

import { chatModelBlockReason, pickActiveModel, useSettingsStore } from './settingsStore';
import { useUIStore } from './uiStore';
import type { ModelEntry } from './settingsStore';

const cloud = (key: string): ModelEntry => ({ key, name: key });
const local = (key: string): ModelEntry => ({ key, name: key, is_local: true });

const MODELS: ModelEntry[] = [cloud('opus4.8'), cloud('sonnet'), local('local_gemma')];

describe('pickActiveModel', () => {
  it('keeps the persisted choice when it is still a valid model (survives refresh)', () => {
    expect(pickActiveModel(MODELS, 'opus4.8', 'sonnet')).toBe('sonnet');
    expect(pickActiveModel(MODELS, 'opus4.8', 'local_gemma')).toBe('local_gemma');
  });

  it('defaults to the on-device model in the UI when nothing is persisted', () => {
    expect(pickActiveModel(MODELS, 'opus4.8', null)).toBe('local_gemma');
  });

  it('ignores a stale persisted model that no longer exists, preferring local', () => {
    expect(pickActiveModel(MODELS, 'opus4.8', 'removed-model')).toBe('local_gemma');
  });

  it('falls back to the backend default when there is no local model (cloud build)', () => {
    const cloudOnly = [cloud('opus4.8'), cloud('sonnet')];
    expect(pickActiveModel(cloudOnly, 'opus4.8', null)).toBe('opus4.8');
    expect(pickActiveModel(cloudOnly, 'opus4.8', 'removed')).toBe('opus4.8');
  });
});

describe('loadModels catalog refresh (live picker updates)', () => {
  const respond = (models: ModelEntry[], def = 'opus4.8') =>
    getMock.mockResolvedValue({ models, default: def });

  beforeEach(() => {
    getMock.mockReset();
    localStorage.clear();
    useSettingsStore.setState({ models: [], selectedModel: 'opus4.8' });
  });

  it('keeps a still-valid selection when the catalog is re-fetched', async () => {
    respond(MODELS);
    await useSettingsStore.getState().loadModels();
    useSettingsStore.getState().setSelectedModel('sonnet'); // persists the choice

    // Config change adds a model; the selection must not move.
    respond([...MODELS, local('local_llama')]);
    await useSettingsStore.getState().loadModels();

    const s = useSettingsStore.getState();
    expect(s.selectedModel).toBe('sonnet');
    expect(s.models.map((m) => m.key)).toContain('local_llama');
  });

  it('falls back like initial selection when the selected model disappears', async () => {
    respond(MODELS);
    await useSettingsStore.getState().loadModels();
    useSettingsStore.getState().setSelectedModel('sonnet');

    // sonnet was disabled/removed from the catalog: fall back local-first.
    respond([cloud('opus4.8'), local('local_gemma')]);
    await useSettingsStore.getState().loadModels();
    expect(useSettingsStore.getState().selectedModel).toBe('local_gemma');
  });

  it('falls back to the backend default on a cloud-only catalog', async () => {
    respond(MODELS);
    await useSettingsStore.getState().loadModels();
    useSettingsStore.getState().setSelectedModel('local_gemma');

    respond([cloud('opus4.8'), cloud('haiku')]);
    await useSettingsStore.getState().loadModels();
    expect(useSettingsStore.getState().selectedModel).toBe('opus4.8');
  });

  it('keeps an unpersisted default selection stable across a refresh', async () => {
    // The user never clicked a model: nothing in localStorage, selection is
    // whatever the initial load picked. A catalog refresh (config save adding
    // a local model) must NOT yank the selection to the local-first default.
    respond([cloud('opus4.8'), cloud('sonnet')]);
    await useSettingsStore.getState().loadModels();
    expect(useSettingsStore.getState().selectedModel).toBe('opus4.8');

    respond([cloud('opus4.8'), cloud('sonnet'), local('local_gemma')]);
    await useSettingsStore.getState().loadModels();
    expect(useSettingsStore.getState().selectedModel).toBe('opus4.8');
  });

  it('after a fallback, a refresh keeps the fallback; a fresh initial load restores the persisted choice', async () => {
    respond(MODELS);
    await useSettingsStore.getState().loadModels();
    useSettingsStore.getState().setSelectedModel('sonnet');

    // Disabled: selection falls back …
    respond([cloud('opus4.8'), local('local_gemma')]);
    await useSettingsStore.getState().loadModels();
    expect(useSettingsStore.getState().selectedModel).toBe('local_gemma');

    // … re-enabled: the LIVE selection stays stable (no surprise jump) …
    respond(MODELS);
    await useSettingsStore.getState().loadModels();
    expect(useSettingsStore.getState().selectedModel).toBe('local_gemma');

    // … while a page reload (initial load: empty list) restores the user's
    // persisted choice, which the fallback never overwrote.
    useSettingsStore.setState({ models: [], selectedModel: 'opus4.8' });
    respond(MODELS);
    await useSettingsStore.getState().loadModels();
    expect(useSettingsStore.getState().selectedModel).toBe('sonnet');
  });
});

describe('setAutoMemory persistence', () => {
  beforeEach(() => {
    putMock.mockReset();
    useSettingsStore.setState({ autoMemory: true });
  });

  it('optimistically flips the toggle and PUTs the auto_memory feature flag', () => {
    putMock.mockResolvedValue({});
    useSettingsStore.getState().setAutoMemory(false);
    expect(useSettingsStore.getState().autoMemory).toBe(false);
    expect(putMock).toHaveBeenCalledWith('/api/feature-flags/auto_memory', { enabled: false });
  });

  it('rolls the toggle back when the write fails', async () => {
    putMock.mockRejectedValue(new Error('network'));
    useSettingsStore.getState().setAutoMemory(false);
    expect(useSettingsStore.getState().autoMemory).toBe(false); // optimistic
    await new Promise((r) => setTimeout(r, 0)); // let the rejection's catch run
    expect(useSettingsStore.getState().autoMemory).toBe(true);  // rolled back
  });
});

// Local mode with no on-device model: /api/models offers nothing, and the store
// must never put a cloud key back into the selection (no hardcoded stand-in, no
// stale persisted key).
describe('an empty Local-mode model list', () => {
  beforeEach(() => {
    getMock.mockReset();
    localStorage.clear();
    useSettingsStore.setState({ models: [], selectedModel: '', needsLocalModel: false });
  });

  it('selects nothing, even with a cloud key persisted from an earlier session', async () => {
    localStorage.setItem('whisper.selectedModel', 'opus5.0');
    getMock.mockResolvedValue({ models: [], default: '', needs_local_model: true });
    await useSettingsStore.getState().loadModels();
    const s = useSettingsStore.getState();
    expect(s.selectedModel).toBe('');
    expect(s.needsLocalModel).toBe(true);
  });

  it('picks the on-device model once one is installed', async () => {
    getMock.mockResolvedValue({ models: [], default: '', needs_local_model: true });
    await useSettingsStore.getState().loadModels();
    getMock.mockResolvedValue({ models: [local('local_gemma')], default: 'local_gemma', needs_local_model: false });
    await useSettingsStore.getState().loadModels();
    const s = useSettingsStore.getState();
    expect(s.selectedModel).toBe('local_gemma');
    expect(s.needsLocalModel).toBe(false);
  });
});

describe('chatModelBlockReason', () => {
  it('lets a selection the list offers through', () => {
    expect(chatModelBlockReason({ models: MODELS, selectedModel: 'sonnet', needsLocalModel: false })).toBeNull();
  });

  it('refuses in Local mode with no on-device model and says where to get one', () => {
    const reason = chatModelBlockReason({ models: [], selectedModel: '', needsLocalModel: true });
    expect(reason).toMatch(/Discover/);
  });

  it('refuses a selection the loaded list does not offer', () => {
    expect(chatModelBlockReason({ models: MODELS, selectedModel: 'gone', needsLocalModel: false })).toBeTruthy();
  });

  it('leaves an unexplained empty list (not loaded yet) to the server', () => {
    expect(chatModelBlockReason({ models: [], selectedModel: '', needsLocalModel: false })).toBeNull();
  });
});

describe('the picker follows the model mode and the workspace', () => {
  beforeEach(() => {
    getMock.mockReset();
    putMock.mockReset();
    useSettingsStore.setState({ models: [], selectedModel: '' });
  });

  it('re-reads the model list and retention only after the mode change is saved', async () => {
    let savePut: (v: unknown) => void = () => {};
    putMock.mockReturnValue(new Promise((r) => { savePut = r; }));
    getMock.mockResolvedValue({ models: [], default: '', needs_local_model: true, mode: '', enabled: false });

    useSettingsStore.getState().setModelMode('local');
    // The backend filters on the PERSISTED mode, so nothing is fetched early.
    expect(getMock).not.toHaveBeenCalled();

    savePut({});
    await new Promise((r) => setTimeout(r, 0));
    const urls = getMock.mock.calls.map((c) => c[0]);
    expect(urls).toContain('/api/models');
    expect(urls).toContain('/api/data-retention');
  });

  it('re-reads the model list when the connected workspace changes', async () => {
    getMock.mockResolvedValue({ models: [cloud('opus5.0')], default: 'opus5.0', needs_local_model: false });
    useUIStore.getState().setWsConnected(true, '/tmp/project-with-its-own-models');
    await new Promise((r) => setTimeout(r, 0));
    expect(getMock.mock.calls.map((c) => c[0])).toContain('/api/models');
  });
});

describe('setModelMode and voice availability', () => {
  const flush = () => new Promise((r) => setTimeout(r, 0));

  beforeEach(() => {
    putMock.mockReset();
    loadStatusMock.mockReset();
    useSettingsStore.setState((s) => ({ config: { ...s.config, modelMode: 'cloud' } }));
  });

  it('re-reads voice availability once the server holds the new mode', async () => {
    putMock.mockResolvedValue({});
    useSettingsStore.getState().setModelMode('local');
    await flush();
    await flush();
    expect(loadStatusMock).toHaveBeenCalledWith(true);
    expect(useSettingsStore.getState().config.modelMode).toBe('local');
  });

  it('leaves voice availability alone when the mode was not saved', async () => {
    putMock.mockRejectedValue(new Error('offline'));
    useSettingsStore.getState().setModelMode('local');
    await flush();
    await flush();
    expect(loadStatusMock).not.toHaveBeenCalled();
    expect(useSettingsStore.getState().config.modelMode).toBe('cloud');
  });
});
