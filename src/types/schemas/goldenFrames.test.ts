import { describe, expect, it } from 'vitest';
import { SSEEventDataSchema } from './chat.schema';

/* The chat frames the server emits are pinned by tests/golden_fixtures. The
   top-level schema is passthrough, so a frame nothing declares still reaches
   the client unvalidated, and nothing reads it: a frame no one consumes is
   legacy (the retired tool_pool frame outlived its Stats tab this way). Every
   key a pinned frame carries must be one this schema declares. */
const fixtures = import.meta.glob('../../../tests/golden_fixtures/*.json', {
  eager: true,
  import: 'default',
}) as Record<string, unknown[]>;

describe('pinned chat frames', () => {
  it('found the golden fixtures', () => {
    expect(Object.keys(fixtures).length).toBeGreaterThan(0);
  });

  it('carry only keys the client schema declares', () => {
    const declared = new Set(Object.keys(SSEEventDataSchema.shape));
    const undeclared = new Set<string>();
    for (const frames of Object.values(fixtures)) {
      for (const frame of frames) {
        if (frame && typeof frame === 'object') {
          for (const key of Object.keys(frame)) if (!declared.has(key)) undeclared.add(key);
        }
      }
    }
    expect([...undeclared]).toEqual([]);
  });
});
