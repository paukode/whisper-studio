import React from 'react';
import { useActiveChatStore } from '@/stores/sessionRuntimes';

/** Token counter reads from chatStore instead of DOM manipulation */
export const TokenCounter: React.FC = () => {
  const inputTokens = useActiveChatStore((s) => s.inputTokens);
  const outputTokens = useActiveChatStore((s) => s.outputTokens);
  const estimatedCost = useActiveChatStore((s) => s.estimatedCost);
  const isVisible = inputTokens > 0 || outputTokens > 0;

  if (!isVisible) return null;

  return (
    <span
      className="token-counter"
      id="tokenCounter"
      title="Tokens used / estimated cost"
    >
      {inputTokens.toLocaleString()} in &middot; {outputTokens.toLocaleString()} out &middot; ${estimatedCost.toFixed(4)}
    </span>
  );
};
