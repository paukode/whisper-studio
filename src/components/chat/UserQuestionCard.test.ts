import { describe, it, expect } from 'vitest';
import { withBrowseOption, isOtherChoice } from './UserQuestionCard';

describe('withBrowseOption — malformed options never crash the chat', () => {
  // Regression: a model can emit `options` as undefined, null, or a string
  // (the schema asks for an array but the value is trusted). This used to
  // throw `options.some is not a function` and take down the whole chat via
  // the error boundary. All non-array inputs must coerce to a safe list.
  it('coerces undefined to an empty list', () => {
    expect(withBrowseOption('which file?', undefined as unknown as string[])).toEqual([]);
  });

  it('coerces null to an empty list', () => {
    expect(withBrowseOption('which file?', null as unknown as string[])).toEqual([]);
  });

  it('coerces a string to an empty list', () => {
    // A string has no .some — this is the exact shape that crashed.
    expect(withBrowseOption('pick one', 'a, b, c' as unknown as string[])).toEqual([]);
  });

  it('leaves a normal option list intact for a non-pathy question', () => {
    expect(withBrowseOption('favourite colour?', ['red', 'blue'])).toEqual(['red', 'blue']);
  });

  it('injects Browse… for a pathy question', () => {
    const out = withBrowseOption('which folder should I save to?', ['~/Documents']);
    expect(out).toContain('Browse…');
  });

  it('does not double-inject when the model already offered a Browse option', () => {
    const out = withBrowseOption('where to save?', ['Browse for a folder', '~/x']);
    expect(out).toEqual(['Browse for a folder', '~/x']);
  });
});

describe('isOtherChoice', () => {
  it('matches the Other sentinel but not real choices starting with "other"', () => {
    expect(isOtherChoice('Other')).toBe(true);
    expect(isOtherChoice('Other (please specify)')).toBe(true);
    expect(isOtherChoice('Otherton, Jane')).toBe(false);
    expect(isOtherChoice('Other Inc.')).toBe(false);
  });
});
