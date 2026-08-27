import { describe, expect, it } from 'vitest';
import { isNearBottom } from './TranscriptionPanel';

describe('isNearBottom', () => {
  it('follows while at (or within slack of) the bottom', () => {
    expect(isNearBottom({ scrollTop: 1000, scrollHeight: 1600, clientHeight: 600 })).toBe(true);
    expect(isNearBottom({ scrollTop: 960, scrollHeight: 1600, clientHeight: 600 })).toBe(true);
  });

  it('stops following once the user scrolls up past the slack', () => {
    expect(isNearBottom({ scrollTop: 900, scrollHeight: 1600, clientHeight: 600 })).toBe(false);
    expect(isNearBottom({ scrollTop: 0, scrollHeight: 1600, clientHeight: 600 })).toBe(false);
  });

  it('treats an unscrollable pane as at-bottom', () => {
    expect(isNearBottom({ scrollTop: 0, scrollHeight: 400, clientHeight: 600 })).toBe(true);
  });
});
