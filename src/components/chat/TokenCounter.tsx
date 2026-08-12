import React from 'react';
import { useActiveChatStore } from '@/stores/sessionRuntimes';

/** 940 · 4.0K · 118K · 1.2M — compact enough that a session-long total fits the
 *  composer strip without wrapping, exact below 1K where the digits still fit. */
export const formatTokenCount = (n: number): string => {
  if (n < 1000) return String(Math.max(0, Math.round(n)));
  if (n < 1e6) {
    const k = n / 1000;
    return `${k < 100 ? k.toFixed(1) : Math.round(k)}K`;
  }
  return `${(n / 1e6).toFixed(1)}M`;
};

/**
 * The session's token/cost readout, under the composer.
 *
 * One readout for the whole app: tokens in, tokens out, how full the model's
 * context window is, and cost so far. The status strip below the terminal used
 * to repeat the same three numbers from the same store fields, so it now
 * carries only workspace state (model, effort, git, tasks).
 *
 * Tokens and cost are SESSION totals — every turn added up, rehydrated from the
 * server's recorded spend when a session is reopened. The context percentage is
 * necessarily per-turn: it measures the last prompt against the window, and a
 * session sends that window many times over.
 */
export const TokenCounter: React.FC = () => {
  const inputTokens = useActiveChatStore((s) => s.sessionInputTokens);
  const outputTokens = useActiveChatStore((s) => s.sessionOutputTokens);
  const cost = useActiveChatStore((s) => s.sessionCost);
  const contextUsed = useActiveChatStore((s) => s.contextUsed);
  const contextMax = useActiveChatStore((s) => s.contextMax);

  if (inputTokens <= 0 && outputTokens <= 0) return null;

  const contextPct =
    contextMax > 0 ? Math.min(100, Math.round((contextUsed / contextMax) * 100)) : 0;

  return (
    <span
      className="token-counter"
      id="tokenCounter"
      title={
        `Session: ${inputTokens.toLocaleString()} prompt tokens in, ` +
        `${outputTokens.toLocaleString()} out, $${cost.toFixed(4)}` +
        (contextMax > 0
          ? `\nLast turn used ${contextUsed.toLocaleString()} of the model's ` +
            `${contextMax.toLocaleString()}-token context window`
          : '')
      }
    >
      {formatTokenCount(inputTokens)} in
      <span className="tc-sep">&middot;</span>
      {formatTokenCount(outputTokens)} out
      {contextMax > 0 && (
        <>
          <span className="tc-sep">&middot;</span>
          <span className="tc-ctx-track" aria-hidden="true">
            <span
              className={`tc-ctx-fill${contextPct >= 80 ? ' hot' : ''}`}
              style={{ width: `${contextPct}%` }}
            />
          </span>
          {contextPct}% ctx
        </>
      )}
      <span className="tc-sep">&middot;</span>${cost.toFixed(4)}
    </span>
  );
};
