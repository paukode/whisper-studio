/* One line per effort tier and per on-device thinking level: what the level
 * means for speed, cost, and quality. Shown under the Thinking control in the
 * composer's "How it thinks" panel. */

/** Accurate to server/infrastructure/effort.py: extra and ultracode both send
 *  xhigh, max is the deepest raw reasoning, ultracode adds parallel
 *  orchestration. */
export const EFFORT_NOTES: Record<string, string> = {
  none: 'No thinking. Fastest and cheapest; quality drops on anything tricky.',
  low: 'Light reasoning. Fast and cheap; good for small, clear-cut tasks.',
  medium: 'Moderate reasoning. Balanced speed, cost, and quality for everyday work.',
  high: 'Thorough reasoning; the default. Slower and costlier, reliable on hard tasks.',
  extra: 'Deep reasoning (xhigh). Noticeably slower and pricier; strong on complex work.',
  max: 'Deepest reasoning, no token cap. Slowest and most expensive; peak single-model quality.',
  ultracode: 'Deep reasoning plus parallel subagents. Slowest overall; highest quality and cost, heavy token use.',
};

/** Accurate to server/local/thinking.py: llama-server caps the thinking at
 *  these token counts, a model with its own levels (gpt-oss) gets the level
 *  itself. */
export const THINKING_NOTES: Record<string, string> = {
  none: 'No thinking. The model answers at once; fastest.',
  low: 'Short thinking, up to about 1K tokens. Fast; good for easy questions.',
  medium: 'Moderate thinking, up to about 4K tokens. Balanced for everyday work.',
  high: 'Long thinking, up to about 16K tokens. Slower; for hard problems.',
  max: 'Thinks as long as the model needs, no cap; the default. Slowest.',
};
