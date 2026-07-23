import { describe, it, expect, vi, beforeEach } from 'vitest';

// settingsStore.setAutoMemory persists via put() from the api client.
const { putMock, getMock } = vi.hoisted(() => ({ putMock: vi.fn(), getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: putMock }));

import { pickActiveModel, useSettingsStore } from './settingsStore';
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
