/**
 * The empty-answer fallback, shared by BOTH turn paths: the fresh turn
 * (useChatStream's send) and the approval resume (sseStream's
 * sendApprovalContinuation).
 *
 * A round can legitimately end with no assistant text — the model stops after
 * seeing a tool error, or ends the turn with an empty output (the engine then
 * emits [DONE] with no text frame at all). Committing nothing in that case
 * renders as total silence: no message, no error, no card, streaming just
 * switches off, and the user has to nudge the model by hand to find out the
 * turn is over. Both paths therefore synthesize a line saying what happened.
 *
 * Returns '' when there is genuinely nothing to report (no SSE events seen at
 * all), so callers can keep treating "no text" as "commit nothing".
 */

/** The per-leg stream signals this reads off the chat store. */
export interface EmptyResponseSignals {
  lastToolError: string | null;
  lastToolName: string | null;
  lastToolOutput: string | null;
  sseEventCount: number;
}

export function emptyResponseFallback(s: EmptyResponseSignals): string {
  if (s.lastToolError) {
    return `**${s.lastToolName ?? 'tool'}** returned an error:\n\n\`\`\`\n${s.lastToolError}\n\`\`\`\n\n*(The model sent no text response. This usually means it stopped after seeing the tool error. Ask again or rephrase.)*`;
  }
  if (s.lastToolOutput) {
    const preview = s.lastToolOutput.length > 800
      ? s.lastToolOutput.substring(0, 800) + '…'
      : s.lastToolOutput;
    return `*(No text response from the model.)*\n\nLast tool: **${s.lastToolName ?? '(unknown)'}**\n\`\`\`\n${preview}\n\`\`\``;
  }
  if (s.sseEventCount > 0) {
    return `*(The model ended the turn without text or tool calls. SSE events received: ${s.sseEventCount}. Inspect \`window.__lastSSE\` in DevTools for details.)*`;
  }
  return '';
}
