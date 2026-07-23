import React, { useEffect, useRef, useState } from 'react';
import { useActiveChatStore } from '@/stores/sessionRuntimes';
import { useSettingsStore } from '@/stores/settingsStore';
import { StreamingMarkdown } from '@/components/markdown/StreamingMarkdown';
import { AgentCard, groupAgentTools, groupForActivity, isActivityEntry, isTasksEntry, friendlyToolName, ACTIVITY_MIN_RUN } from '@/components/chat/AgentCard';
import { TaskCard } from '@/components/chat/TaskCard';
import { ActivityRow } from '@/components/chat/ActivityRow';
import { TeamReportCard } from '@/components/chat/TeamReportCard';
import { findMatchingTeamReports } from '@/hooks/chatStream/teamProgress';

export interface StreamingMessageProps {
  content: string;
  isStreaming: boolean;
}

/**
 * Renders a streaming assistant message matching the original:
 *  - Live elapsed timer during thinking (100ms updates, "X.Ys")
 *  - Token meta display ("1.2s . 1,234 in . 5,678 out")
 *  - Collapse thinking on first text token
 *  - Remove empty thinking block if no text arrives
 */
export const StreamingMessage: React.FC<StreamingMessageProps> = ({ content, isStreaming }) => {
  const thinkingContent = useActiveChatStore((s) => s.currentThinkingContent);
  const thinkingStartTime = useActiveChatStore((s) => s.thinkingStartTime);
  const thinkingElapsedMs = useActiveChatStore((s) => s.thinkingElapsedMs);
  const inputTokens = useActiveChatStore((s) => s.inputTokens);
  const outputTokens = useActiveChatStore((s) => s.outputTokens);
  const streamToolUse = useActiveChatStore((s) => s.currentStreamToolUse);
  // Turn-local team reports, folded live from team_progress SSE events.
  // Rendering them here is what makes agents visibly run in parallel DURING
  // the turn — before this, the streaming view only knew the generic
  // AgentCard and every live event was dropped.
  const liveTeamReports = useActiveChatStore((s) => s.liveTeamReports);
  // Local on-device models don't emit thinking — the pre-first-token wait is
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

  return (
    <div className="chat-msg-wrap assistant-wrap">
      <div className="chat-msg assistant" role="article" aria-live="polite">
        {/* Thinking block — shown immediately when streaming starts */}
        {((isStreaming && !content) || thinkingContent) && (
          <details className="thinking-block" open={!content}>
            <summary style={{
              cursor: 'pointer',
              fontSize: '0.85em',
              color: 'var(--text-muted)',
              padding: '4px 0',
              userSelect: 'none',
              display: 'flex',
              alignItems: 'center',
              gap: 6,
            }}>
              <span>{(isLocalModel && !thinkingContent) ? '\u2728' : '\uD83D\uDCAD'}</span>
              {content
                ? 'Thought process'
                : thinkingContent
                  ? 'Thinking\u2026'
                  : (isLocalModel ? 'Generating\u2026' : 'Thinking\u2026')}
              {!content && <span className="pulse-dot"></span>}
              {displayElapsed > 0 && (
                <span style={{ marginLeft: 'auto', fontSize: '0.85em', opacity: 0.7 }}>
                  {formatMs(displayElapsed)}
                </span>
              )}
            </summary>
            {thinkingContent && (
              <div style={{
                fontSize: '0.85em',
                color: 'var(--text-muted)',
                padding: '8px 12px',
                background: 'var(--bg-secondary)',
                borderRadius: 6,
                marginTop: 4,
                marginBottom: 8,
                maxHeight: 300,
                overflow: 'auto',
                whiteSpace: 'pre-wrap',
                lineHeight: 1.5,
              }}>
                {thinkingContent}
              </div>
            )}
          </details>
        )}

        {/* Real-time tool traces — shown as tools are called during streaming */}
        {streamToolUse.length > 0 && (
          <div className="skill-traces">
            {/* ACTIVITY_MIN_RUN so the collapsed Activity row appears from tool
             *  #1 and counts up live, instead of flashing individual cards that
             *  snap into one row the moment the stream commits to chatStore. */}
            {groupForActivity(groupAgentTools(streamToolUse), { minRun: ACTIVITY_MIN_RUN }).map((entry, idx) => {
              if (Array.isArray(entry)) {
                // A team_create or spawn_agent group with live reports renders
                // the rich per-agent cards in place of the generic AgentCard,
                // exactly like the committed view does — one card per report,
                // never both card types for the same group.
                const liveReports = findMatchingTeamReports(entry, liveTeamReports);
                if (liveReports.length > 0) {
                  return liveReports.map((r) => (
                    <TeamReportCard key={`team-${r.team_id}-${idx}`} report={r} />
                  ));
                }
                return <AgentCard key={`agent-group-${idx}`} tools={entry} />;
              }
              if (isTasksEntry(entry)) {
                return <TaskCard key={`tasks-${idx}`} tools={entry.tools} />;
              }
              if (isActivityEntry(entry)) {
                return <ActivityRow key={`activity-${idx}`} tools={entry.tools} />;
              }
              const tool = entry;
              const pathHint = (tool.input as Record<string, unknown>)?.path;
              const label = friendlyToolName(tool.toolName);
              const displayName = pathHint && typeof pathHint === 'string'
                ? `${label}: ${pathHint.split('/').pop()}`
                : label;
              return (
              <details key={`${tool.toolName}-${idx}`} className={`skill-trace ${tool.status === 'complete' ? 'done' : tool.status}`}>
                <summary className="skill-trace-summary">
                  <span className="skill-trace-icon">{'\uD83D\uDD27'}</span>
                  <span className="skill-trace-name">{displayName}</span>
                  <span className="skill-trace-status">
                    {tool.status === 'complete' ? (
                      <span className="trace-check">{'\u2713'}</span>
                    ) : tool.status === 'error' ? (
                      <span className="trace-check" style={{ color: 'var(--error, #f87171)' }}>{'\u2715'}</span>
                    ) : (
                      <span className="trace-spinner">{'\u27F3'}</span>
                    )}
                  </span>
                </summary>
                {tool.result && (
                  <div className="skill-trace-output">
                    <pre>{tool.result.length > 2000 ? tool.result.slice(0, 2000) + '\u2026' : tool.result}</pre>
                  </div>
                )}
              </details>
              );
            })}
          </div>
        )}

        {/* Live team reports with no team_create trace to anchor to (events
         *  can beat the tool trace over the wire) still render — a running
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
                <TeamReportCard key={`live-team-${r.team_id}`} report={r} />
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
          <StreamingMarkdown content={content} isStreaming={isStreaming} stepFormat />
        )}

        {/* Live activity footer — keeps motion visible during the quiet
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
                return running ? `Running ${running.toolName}…` : 'Working…';
              })()}
            </span>
          </div>
        )}
      </div>
    </div>
  );
};
