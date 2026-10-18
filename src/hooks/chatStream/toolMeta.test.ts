/**
 * The row's ToolMeta from the server's two reports of one: the skill_result
 * frame's meta and an approved write's diff stats. Every key is present on
 * the row, null when the server sent no number for it.
 */
import { describe, expect, it } from 'vitest';
import { toolMetaFromFrame, toolMetaWithStats } from './toolMeta';

describe('toolMetaFromFrame', () => {
  it('camelCases every key and keeps the nulls', () => {
    expect(toolMetaFromFrame({
      duration_ms: 5,
      output_chars: 10,
      truncated: false,
      lines: null,
      bytes: null,
      added: null,
      removed: null,
    })).toEqual({
      durationMs: 5,
      outputChars: 10,
      truncated: false,
      lines: null,
      bytes: null,
      added: null,
      removed: null,
    });
  });

  it('is undefined for a frame without a meta, and null for a key it lacks', () => {
    expect(toolMetaFromFrame(undefined)).toBeUndefined();
    expect(toolMetaFromFrame({ added: 2 })).toEqual({
      durationMs: null,
      outputChars: null,
      truncated: null,
      lines: null,
      bytes: null,
      added: 2,
      removed: null,
    });
  });

  it('reads a value of the wrong type as unknown, never as a number', () => {
    // A frame that failed validation reaches the handler raw.
    const raw = { duration_ms: 'slow', truncated: 'yes', lines: Number.NaN } as never;
    expect(toolMetaFromFrame(raw)).toMatchObject({ durationMs: null, truncated: null, lines: null });
  });
});

describe('toolMetaWithStats', () => {
  it('takes the write numbers from the stats and keeps what the row had', () => {
    // The stats' line count is the written file's, not the lines a read
    // returned, so it stays out of the meta.
    const before = {
      durationMs: 9,
      outputChars: 30,
      truncated: false,
      lines: null,
      bytes: null,
      added: null,
      removed: null,
    };
    expect(toolMetaWithStats(before, { added: 3, removed: 1, lines: 12, bytes: 240, stored: true })).toEqual({
      durationMs: 9,
      outputChars: 30,
      truncated: false,
      lines: null,
      bytes: 240,
      added: 3,
      removed: 1,
    });
    expect(toolMetaWithStats(undefined, { added: 1 })).toEqual({
      durationMs: null,
      outputChars: null,
      truncated: null,
      lines: null,
      bytes: null,
      added: 1,
      removed: null,
    });
  });
});
