import { describe, expect, it } from 'vitest';
import { confidenceLabel, factSentence } from './factSentence';

describe('factSentence', () => {
  it('puts the page entity first for outgoing facts', () => {
    expect(factSentence('Dana Kim', 'Northwind Bank', 'works_at', 'out')).toBe(
      'Dana Kim works at Northwind Bank.',
    );
  });

  it('puts the other entity first for incoming facts', () => {
    // The fact hired(Northwind Bank -> Dana Kim) read from Dana's page.
    expect(factSentence('Dana Kim', 'Northwind Bank', 'hired', 'in')).toBe(
      'Northwind Bank hired Dana Kim.',
    );
  });

  it('falls back to the raw predicate as words for unknown predicates', () => {
    expect(factSentence('A', 'B', 'shipped_with', 'out')).toBe('A shipped with B.');
  });
});

describe('confidenceLabel', () => {
  it('reads confirmed at three files and states below', () => {
    expect(confidenceLabel(3, 3)).toBe('confirmed in 3 files');
    expect(confidenceLabel(2, 2)).toBe('stated in 2 files');
    expect(confidenceLabel(1, 2)).toBe('stated in 1 file');
    expect(confidenceLabel(1, 1)).toBe('stated once');
  });
});
