/**
 * Render a typed fact as a plain sentence: "Dana Kim works at Northwind Bank".
 *
 * VERB_PHRASES must cover the closed predicate vocabulary in
 * server/index/relations_vocab.py (tests/test_explore_views.py verifies the two
 * stay in sync). Direction is expressed purely by word order: 'out' puts the
 * page's own entity first, 'in' puts the other entity first — no arrows.
 */

export const VERB_PHRASES: Record<string, string> = {
  works_at: 'works at',
  reports_to: 'reports to',
  member_of: 'is a member of',
  collaborated_with: 'collaborated with',
  mentored: 'mentored',
  has_role: 'has the role of',
  hired: 'hired',
  customer_of: 'is a customer of',
  party_to: 'is a party to',
  owns: 'owns',
  authored: 'authored',
  contributed_to: 'contributed to',
  launched: 'launched',
  uses: 'uses',
  depends_on: 'depends on',
  part_of: 'is part of',
  competitor_of: 'competes with',
  achieved: 'achieved',
  located_in: 'is located in',
  attended: 'attended',
  related_to: 'is related to',
};

export function factSentence(
  self: string,
  other: string,
  predicate: string,
  direction: 'out' | 'in',
): string {
  const verb = VERB_PHRASES[predicate] ?? predicate.replace(/_/g, ' ');
  const [a, b] = direction === 'out' ? [self, other] : [other, self];
  return `${a} ${verb} ${b}.`;
}

/** Confidence in words, from the distinct source-file count: repetition across
 * files reinforces a fact, repetition inside one file does not. */
export function confidenceLabel(files: number, sources: number): string {
  if (files >= 3) return `confirmed in ${files} files`;
  if (files === 2) return 'stated in 2 files';
  return sources > 1 ? 'stated in 1 file' : 'stated once';
}
