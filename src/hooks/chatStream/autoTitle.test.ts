/**
 * The automatic title: a good reply sets the title, a failed one is logged
 * and marks the session so the window does not ask again after every turn.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { _resetAutoTitle, autoTitleFailed, requestAutoTitle } from './autoTitle';

const reply = (status: number, body: unknown) =>
  vi.fn().mockResolvedValue({ ok: status < 300, status, json: async () => body });

describe('requestAutoTitle', () => {
  beforeEach(() => {
    _resetAutoTitle();
    vi.spyOn(console, 'error').mockImplementation(() => {});
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('hands a returned title to the caller and does not mark the session', async () => {
    vi.stubGlobal('fetch', reply(200, { title: 'Trip plan' }));
    const onTitle = vi.fn();
    await requestAutoTitle('s1', 'User: hi', onTitle);
    expect(onTitle).toHaveBeenCalledWith('Trip plan');
    expect(autoTitleFailed('s1')).toBe(false);
  });

  it('logs a failed request and marks only that session', async () => {
    vi.stubGlobal('fetch', reply(502, { error: 'Title generation failed: boom' }));
    const onTitle = vi.fn();
    await requestAutoTitle('s1', 'User: hi', onTitle);
    expect(onTitle).not.toHaveBeenCalled();
    expect(autoTitleFailed('s1')).toBe(true);
    expect(autoTitleFailed('s2')).toBe(false);
    expect(console.error).toHaveBeenCalled();
  });

  it('marks the session when the request itself fails', async () => {
    vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('network')));
    await requestAutoTitle('s1', 'User: hi', vi.fn());
    expect(autoTitleFailed('s1')).toBe(true);
  });
});
