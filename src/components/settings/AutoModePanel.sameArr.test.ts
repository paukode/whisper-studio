import { describe, expect, it } from 'vitest';
import { sameArr } from './AutoModePanel';

// Regression: the panel used to detect "custom rules" with a reference check
// (`effective.allow !== defaults.allow`). Effective and default rule sets always
// arrive as separate JSON arrays, so that reference is ALWAYS unequal — the
// panel showed "custom" even when the rules matched the defaults. sameArr does a
// content comparison instead; `!sameArr(...)` is the real "is custom" signal.
describe('AutoModePanel — sameArr (custom-rule detection)', () => {
  it('treats equal-content arrays as the same even when they are distinct instances', () => {
    const effective = ['Bash(ls*)', 'Read'];
    const defaults = ['Bash(ls*)', 'Read'];
    expect(effective).not.toBe(defaults); // the bug's trigger: distinct instances
    expect(sameArr(effective, defaults)).toBe(true);
    expect(!sameArr(effective, defaults)).toBe(false); // => NOT flagged custom
  });

  it('flags a differing element as custom', () => {
    expect(sameArr(['a', 'b'], ['a', 'c'])).toBe(false);
  });

  it('flags a differing length as custom (both directions)', () => {
    expect(sameArr(['a'], ['a', 'b'])).toBe(false);
    expect(sameArr(['a', 'b'], ['a'])).toBe(false);
  });

  it('treats two empty arrays as equal (empty user section keeps the default)', () => {
    expect(sameArr([], [])).toBe(true);
  });

  it('is order-sensitive: reordered rules count as custom', () => {
    expect(sameArr(['a', 'b'], ['b', 'a'])).toBe(false);
  });
});
