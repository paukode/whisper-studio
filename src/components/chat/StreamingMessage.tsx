import React, { useEffect, useRef, useState } from 'react';
import { useActiveChatStore } from '@/stores/sessionRuntimes';
import { useSettingsStore } from '@/stores/settingsStore';
import { StreamingMarkdown } from '@/components/markdown/StreamingMarkdown';
import { AgentCard, groupAgentTools, groupForActivity, isActivityEntry, isTasksEntry, ACTIVITY_MIN_RUN } from '@/components/chat/AgentCard';
import { TaskCard } from '@/components/chat/TaskCard';
import { ActivityRow } from '@/components/chat/ActivityRow';
import { ICON } from '@/components/chat/activityIcons';
import { TeamAgentRows } from '@/components/chat/AgentRows';
import { DeliveryChips } from '@/components/chat/DeliveryChips';
import { findMatchingTeamReports } from '@/hooks/chatStream/teamProgress';
import { mergeDeliveries } from '@/hooks/chatStream/deliveries';
import { liveCaption } from '@/hooks/chatStream/activityLabels';
import type { Delivery } from '@/types/chat';

export interface StreamingMessageProps {
  content: string;
  isStreaming: boolean;
  /** Inside a turn's bubble (TurnBubble): the live tail of a turn that
   *  already committed rows, drawn with no bubble of its own. */
  embedded?: boolean;
  /** The deliveries the turn's committed rows carry: the tail shows the
   *  turn's one Verified row, these merged with the ones still arriving. */
  priorDeliveries?: readonly Delivery[];
}

/**
 * Renders a streaming assistant message matching the original:
 *  - Its own bubble for a turn with nothing committed yet, or, embedded, the
 *    tail of the turn's bubble
 *  - Live elapsed timer during thinking (100ms updates, "X.Ys")
 *  - Token meta display ("1.2s . 1,234 in . 5,678 out")
 *  - Collapse thinking on first text token
 *  - Remove empty thinking block if no text arrives
 */
export const StreamingMessage: React.FC<StreamingMessageProps> = ({ content, isStreaming, embedded, priorDeliveries }) => {
  const thinkingContent = useActiveChatStore((s) => s.currentThinkingContent);
  const streamStatus = useActiveChatStore((s) => s.streamStatus);
  const thinkingStartTime = useActiveChatStore((s) => s.thinkingStartTime);
  const thinkingElapsedMs = useActiveChatStore((s) => s.thinkingElapsedMs);
  const inputTokens = useActiveChatStore((s) => s.inputTokens);
  const outputTokens = useActiveChatStore((s) => s.outputTokens);
  const streamToolUse = useActiveChatStore((s) => s.currentStreamToolUse);
  // Captions that landed for the live calls: a row shows its caption at
  // once, not only after the commit.
  const streamLabels = useActiveChatStore((s) => s.currentStreamLabels);
  // Turn-local team reports, folded live from team_progress SSE events.
  // Rendering them here is what makes agents visibly run in parallel DURING
  // the turn.
  const liveTeamReports = useActiveChatStore((s) => s.liveTeamReports);
  // Deliveries the server verified in the text streamed so far: the chips the
  // committed message keeps, shown the moment each one is verified.
  const liveDeliveries = useActiveChatStore((s) => s.liveDeliveries);
  // Local on-device models don't emit thinking: the pre-first-token wait is
  // just generation warm-up, so label it honestly rather than "Thinking…".
  const isLocalModel = useSettingsStore((s) => s.models.find((m) => m.key === s.selectedModel)?.is_local ?? false);

  // Live timer
  const [elapsed, setElapsed] = useState(0);
  const timerRef = useRef<ReturnType<typeof setInterval> | null>(null);

  useEffect(() => {
    if (thinkingStartTime && !content) {
      timerRef.current = setInterval(() => {
        setElapsed(performance.now() - thinkingStartTime);
      }, 100);
    } else {
      if (timerRef.current) clearInterval(timerRef.current);
      timerRef.current = null;
    }
    return () => {
      if (timerRef.current) clearInterval(timerRef.current);
    };
  }, [thinkingStartTime, content]);

  const formatMs = (ms: number) => (ms / 1000).toFixed(1) + 's';
  const formatTokens = (n: number) => n.toLocaleString();

  // Determine display elapsed (live or final)
  const displayElapsed = content ? thinkingElapsedMs : elapsed;

  // Token meta line shown after thinking completes
  const showMeta = content && (thinkingElapsedMs > 0 || inputTokens > 0);

  const deliveries = priorDeliveries && priorDeliveries.length > 0
    ? mergeDeliveries(priorDeliveries, liveDeliveries)
    : liveDeliveries;

  const body = (
    <>
      {/* Thinking block, shown immediately when streaming starts */}
      {((isStreaming && !content) || thinkingContent) && (
        <details className="thinking-block" open={!content}>
          <summary className="thinking-summary">
            <span className="activity-slot">{(isLocalModel && !thinkingContent) ? ICON.sparkle : ICON.thought}</span>
            {/* A status outranks the thinking label: thinking tokens clear
             *  it, so one that is set now came after them (the goal check
             *  after a thinking-only round) and names the current phase. */}
            {content
              ? 'Thought process'
              : streamStatus === 'searching'
                ? 'Searching your workspace\u2026'
                : streamStatus === 'connecting'
                  ? 'Waiting for the model\u2026'
                  : streamStatus === 'preparing'
                    ? 'Preparing\u2026'
                    : streamStatus
                      // Free-text phases (compaction, continuation, the goal
                      // check) arrive as human-readable status frames; show
                      // them as-is.
                      ? streamStatus
                      : thinkingContent
                        ? 'Thinking\u2026'
                        : (isLocalModel ? 'Generating\u2026' : 'Thinking\u2026')}
            {!content && <span className="pulse-dot"></span>}
            {displayElapsed > 0 && (
              <span className="thinking-elapsed">{formatMs(displayElapsed)}</span>
            )}
          </summary>
          {thinkingContent && <div className="thinking-body">{thinkingContent}</div>}
        </details>
      )}

      {/* Real-time tool traces, shown as tools are called during streaming */}
      {streamToolUse.length > 0 && (
        <div className="skill-traces">
          {/* ACTIVITY_MIN_RUN so the collapsed Activity row appears from tool
           *  #1 and counts up live, instead of flashing individual cards that
           *  snap into one row the moment the stream commits to chatStore. */}
          {groupForActivity(groupAgentTools(streamToolUse), { minRun: ACTIVITY_MIN_RUN }).map((entry, idx) => {
            if (Array.isArray(entry)) {
              // A team_create or spawn_agent group with live reports renders
              // the agent rows in place of the generic AgentCard, exactly
              // like the committed view does: one set of rows per report,
              // never both for the same group.
              const liveReports = findMatchingTeamReports(entry, liveTeamReports);
              if (liveReports.length > 0) {
                return liveReports.map((r) => (
                  <TeamAgentRows key={`team-${r.team_id}-${idx}`} report={r} />
                ));
              }
              return <AgentCard key={`agent-group-${idx}`} tools={entry} />;
            }
            if (isTasksEntry(entry)) {
              return <TaskCard key={`tasks-${idx}`} tools={entry.tools} />;
            }
            if (isActivityEntry(entry)) {
              return (
                <ActivityRow
                  key={`activity-${idx}`}
                  tools={entry.tools}
                  writtenLabel={liveCaption(streamLabels, entry.tools)}
                />
              );
            }
            // An exempt tool (a screenshot, a picker) draws as a row of one.
            return (
              <ActivityRow
                key={`${entry.toolName}-${idx}`}
                tools={[entry]}
                writtenLabel={liveCaption(streamLabels, [entry])}
              />
            );
          })}
        </div>
      )}

      {/* Live team reports with no team_create trace to anchor to (events
       *  can beat the tool trace over the wire) still render: a running
       *  team must never be invisible. */}
      {Object.keys(liveTeamReports).length > 0 && (
        <div className="skill-traces">
          {Object.values(liveTeamReports)
            .filter((r) => findMatchingTeamReports(
              streamToolUse.filter(
                (t) => t.toolName === 'team_create' || t.toolName === 'spawn_agent',
              ),
              { [r.team_id]: r },
            ).length === 0)
            .map((r) => (
              <TeamAgentRows key={`live-team-${r.team_id}`} report={r} />
            ))}
        </div>
      )}

      {/* Token meta line */}
      {showMeta && (
        <div style={{
          fontSize: '0.75em',
          color: 'var(--text-muted)',
          marginBottom: 6,
          display: 'flex',
          gap: 8,
        }}>
          {thinkingElapsedMs > 0 && <span>{formatMs(thinkingElapsedMs)}</span>}
          {inputTokens > 0 && (
            <>
              <span>{'\u00B7'}</span>
              <span>{formatTokens(inputTokens)} in</span>
            </>
          )}
          {outputTokens > 0 && (
            <>
              <span>{'\u00B7'}</span>
              <span>{formatTokens(outputTokens)} out</span>
            </>
          )}
        </div>
      )}

      {/* Response text */}
      {content && (
        <StreamingMarkdown content={content} isStreaming={isStreaming} />
      )}

      {deliveries.length > 0 && <DeliveryChips deliveries={deliveries} />}

      {/* Live activity footer: keeps motion visible during the quiet
       *  phases (a long-running tool, or the gap between tool rounds while
       *  we wait on the model) so the turn never looks stuck. Suppressed
       *  during the pure pre-text thinking phase, which the thinking block
       *  above already animates with its own pulse + timer. */}
      {isStreaming && (content !== '' || streamToolUse.length > 0) && (
        <div className="stream-working" aria-live="polite">
          <span className="pulse-dot" aria-hidden="true" />
          <span>
            {(() => {
              const running = streamToolUse.find(
                (t) => t.status === 'running' || t.status === 'pending',
              );
              if (running) return `Running ${running.toolName}…`;
              // Mid-turn status frames (continuation after an output-limit
              // cut, compaction between rounds) name the quiet phase; the
              // generic label is the fallback.
              return streamStatus || 'Working…';
            })()}
          </span>
        </div>
      )}
    </>
  );

  if (embedded) {
    return <div className="turn-live" aria-live="polite">{body}</div>;
  }
  return (
    <div className="chat-msg-wrap assistant-wrap">
      <div className="chat-msg assistant" role="article" aria-live="polite">{body}</div>
    </div>
  );
};
