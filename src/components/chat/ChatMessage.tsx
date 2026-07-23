import React, { useState, useCallback, useRef, useLayoutEffect } from 'react';
import type { ChatMessage as ChatMessageType } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { ProgramArtifactCard } from '@/components/chat/ProgramArtifactCard';
import { WorkspacePromptCard } from '@/components/chat/WorkspacePromptCard';
import { AgentCard, groupAgentTools, groupForActivity, isActivityEntry, isTasksEntry, friendlyToolName, ACTIVITY_MIN_RUN } from '@/components/chat/AgentCard';
import { TaskCard } from '@/components/chat/TaskCard';
import { PlanCard } from '@/components/chat/PlanCard';
import type { SessionTask } from '@/stores/taskStore';
import { ActivityRow } from '@/components/chat/ActivityRow';
import { TeamReportCard } from '@/components/chat/TeamReportCard';
import { BackgroundTaskCard } from '@/components/chat/BackgroundTaskCard';
import { CronEventCard } from '@/components/chat/CronEventCard';
import { WorkflowPreviewCard } from '@/components/chat/WorkflowPreviewCard';
import { WorkflowRunCard } from '@/components/chat/WorkflowRunCard';
import { CIStatusCard } from '@/components/chat/CIStatusCard';
import { CIDiagnosisCard } from '@/components/chat/CIDiagnosisCard';
import { PreviewScreenshotCard } from '@/components/chat/PreviewScreenshotCard';
import { findMatchingTeamReports } from '@/hooks/chatStream/teamProgress';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import { formatMessageTimestamp } from '@/utils/formatTimestamp';
import { UserQuestionCard, UserQuestionGroupCard } from '@/components/chat/UserQuestionCard';
import { extractFlatCronPayload, exportSingleMessage, copyRichText } from '@/components/chat/messageActions';

export interface ChatMessageProps {
  message: ChatMessageType;
  index: number;
  /** Cumulative task checkpoint for this message's Tasks row, or null when the
   *  row should be suppressed (state unchanged since the previous task turn).
   *  Computed once at the conversation level by `computeTaskCheckpoints`. */
  taskCheckpoint?: SessionTask[] | null;
  /** When true, suppress the msgIn entrance animation on this message's bubble.
   *  ChatPanel sets it for the message that was just streaming so the committed
   *  bubble doesn't replay the entrance the StreamingMessage already showed. */
  noEnter?: boolean;
}

/**
 * Renders a chat message matching the vanilla HTML structure.
 *
 * Structure:
 *   div.chat-msg-wrap.user-wrap | div.chat-msg-wrap.assistant-wrap
 *     div.chat-msg.user | div.chat-msg.assistant
 *       (content)
 */
export const ChatMessage: React.FC<ChatMessageProps> = ({ message, index, taskCheckpoint, noEnter }) => {
  const isCronEvent = message.role === 'cron_event';
  const isUser = message.role === 'user';
  const [isEditing, setIsEditing] = useState(false);
  const [editText, setEditText] = useState('');
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const handleEdit = useCallback(() => {
    setEditText(message.content);
    setIsEditing(true);
    requestAnimationFrame(() => {
      const ta = textareaRef.current;
      if (!ta) return;
      ta.focus();
      // Place caret at the end so users continue typing where they
      // left off rather than overwriting from the start.
      const len = ta.value.length;
      ta.setSelectionRange(len, len);
    });
  }, [message.content]);

  // Auto-grow the edit textarea so it always fits its content without
  // a scrollbar until the 50vh cap kicks in. Reset height to 'auto'
  // first so shrinking text also collapses the box.
  const autoResizeTextarea = useCallback(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    ta.style.height = 'auto';
    const cap = Math.round(window.innerHeight * 0.5);
    ta.style.height = `${Math.min(ta.scrollHeight, cap)}px`;
  }, []);

  // useLayoutEffect runs before the browser paints, so the textarea
  // never visibly "pops" to its final size on entry or on each keystroke.
  useLayoutEffect(() => {
    if (isEditing) autoResizeTextarea();
  }, [isEditing, editText, autoResizeTextarea]);

  const handleEditConfirm = useCallback(() => {
    const trimmed = editText.trim();
    setIsEditing(false);
    if (!trimmed || trimmed === message.content) return;
    window.dispatchEvent(new CustomEvent('whisper-regenerate', {
      detail: {
        index,
        content: trimmed,
        attachmentIds: message.attachmentIds,
        attachmentNames: message.attachmentNames,
      },
    }));
  }, [editText, message.content, message.attachmentIds, message.attachmentNames, index]);

  const handleEditCancel = useCallback(() => {
    setIsEditing(false);
  }, []);

  const handleRegenerate = useCallback(() => {
    if (isUser) {
      window.dispatchEvent(new CustomEvent('whisper-regenerate', {
        detail: {
          index,
          content: message.content,
          attachmentIds: message.attachmentIds,
          attachmentNames: message.attachmentNames,
        },
      }));
    } else {
      // For assistant messages, re-send the preceding user message — carry
      // its attachments through so the regenerate sees the same files.
      const messages = getActiveChatStore().getState().messages;
      const prevUserMsg = messages.slice(0, index).reverse().find(m => m.role === 'user');
      if (prevUserMsg) {
        const prevIndex = messages.indexOf(prevUserMsg);
        window.dispatchEvent(new CustomEvent('whisper-regenerate', {
          detail: {
            index: prevIndex + 1,
            content: prevUserMsg.content,
            attachmentIds: prevUserMsg.attachmentIds,
            attachmentNames: prevUserMsg.attachmentNames,
          },
        }));
      }
    }
  }, [isUser, index, message.content, message.attachmentIds, message.attachmentNames]);

  const handleDelete = useCallback(() => {
    if (isUser) {
      getActiveChatStore().getState().deleteMessagesFrom(index);
    } else {
      getActiveChatStore().getState().deleteMessage(index);
    }
  }, [isUser, index]);

  // cron_event rows render as a standalone card — none of the
  // user/assistant chrome (edit, regenerate, copy, tool traces,
  // attachments, etc.) applies. Hooks above are still called on every
  // render so React's rules-of-hooks stay satisfied. The payload was
  // delivered either live via SSE (useChatStream → addMessage) or by
  // session resume (chat_history JSON).
  //
  // Tolerate two shapes for back-compat with already-persisted rows:
  //   - nested:  { role: 'cron_event', cronEvent: {event_type, ...} }
  //   - flat:    { role: 'cron_event', event_type, cron_id, cron_name, ... }
  // Newly-emitted events use the nested shape; the flat fallback rescues
  // rows written before that fix landed.
  if (isCronEvent) {
    const payload = message.cronEvent ?? extractFlatCronPayload(message);
    if (payload) {
      return <CronEventCard event={payload} />;
    }
  }

  // Background-task lifecycle rows (shell/agent/workflow) — same UI-only
  // contract as cron events, rendered as a compact card.
  if (message.role === 'task_event') {
    if (message.taskEvent) {
      return <BackgroundTaskCard event={message.taskEvent} />;
    }
    return null;
  }

  return (
    <div className={`chat-msg-wrap ${isUser ? 'user-wrap' : 'assistant-wrap'}`}>
      <div className={`chat-msg ${message.role}${isEditing ? ' editing' : ''}${noEnter ? ' no-enter' : ''}`}>
        {/* Thinking block for completed messages */}
        {!isUser && message._thinkingText && (
          <details className="thinking-block">
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
              <span>{'\uD83D\uDCAD'}</span>
              Thought process
              {message._thinkingMs != null && message._thinkingMs > 0 && (
                <span style={{ marginLeft: 'auto', fontSize: '0.85em', opacity: 0.7 }}>
                  {(message._thinkingMs / 1000).toFixed(1)}s
                </span>
              )}
            </summary>
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
              {message._thinkingText}
            </div>
          </details>
        )}

        {/* Index-grounding summary: makes it visible whether this turn was
         *  answered from the workspace index (and how much), so a silent
         *  no-grounding can't be mistaken for "the model couldn't find it".
         *  Only present when at least one index was searched (the backend
         *  omits it otherwise), so users without indexes never see a chip. */}
        {!isUser && message.grounding && message.grounding.searched > 0 && (
          <div style={{
            fontSize: '0.8em',
            color: 'var(--text-muted)',
            padding: '2px 0 6px',
            display: 'flex',
            alignItems: 'center',
            gap: 6,
          }}>
            <span aria-hidden="true">{'🔎'}</span>
            {message.grounding.passages > 0
              ? `Grounded in ${message.grounding.searched} indexed folder${message.grounding.searched === 1 ? '' : 's'} · ${message.grounding.passages} passage${message.grounding.passages === 1 ? '' : 's'}`
              : `Searched ${message.grounding.searched} indexed folder${message.grounding.searched === 1 ? '' : 's'} · no matches`}
          </div>
        )}

        {/* Render tool/skill traces for assistant messages. Two-pass grouping:
         *  1. ``groupAgentTools`` bundles consecutive agent-orchestration calls
         *     (spawn_agent / send_message / team_*) into a single AgentCard.
         *  2. ``groupForActivity`` then bundles consecutive *individual* tool
         *     calls (read / grep / bash / edit / …) into a single <ActivityRow>
         *     — "▸ Activity · 8 steps · ✓" — that expands inline on click.
         *  Special tools (workspace picker, ask-user) pass through both passes
         *  and keep their own chrome.
         *
         *  ACTIVITY_MIN_RUN — MUST match StreamingMessage (it does, by
         *  construction: both import the shared constant). Tool grouping is a
         *  pure transform of message.toolUse (which is persisted verbatim in
         *  the session's chat_history), so using the same threshold here means
         *  the Activity row that appeared while streaming stays put once the
         *  turn completes AND when an old session is restored. With the
         *  default (2), a 1-tool turn would collapse during streaming then pop
         *  back to a bare chip on commit/reload — the disappearing-row bug. */}
        {!isUser && message.toolUse && message.toolUse.length > 0 && (
          <div className="skill-traces">
            {groupForActivity(groupAgentTools(message.toolUse), { minRun: ACTIVITY_MIN_RUN }).map((entry, idx) => {
              if (Array.isArray(entry)) {
                // A group can anchor SEVERAL reports (team_create plus each
                // inline spawn_agent's one-member team) — render them all, and
                // the generic AgentCard only when none matched. Rendering both
                // for the same group double-carded every inline spawn.
                const matchedReports = findMatchingTeamReports(entry, message.teamReports);
                if (matchedReports.length > 0) {
                  return matchedReports.map((r) => (
                    <TeamReportCard key={`team-${r.team_id}-${idx}`} report={r} />
                  ));
                }
                return <AgentCard key={`agent-group-${idx}`} tools={entry} />;
              }
              // Task-management run — render the compact Tasks row only at a
              // checkpoint where the cumulative state changed (taskCheckpoint
              // non-null); otherwise suppress it so the same "N/M done" row
              // doesn't repeat on every turn. The full list is in the drawer.
              if (isTasksEntry(entry)) {
                return taskCheckpoint && taskCheckpoint.length > 0
                  ? <TaskCard key={`tasks-${idx}`} tasks={taskCheckpoint} />
                  : null;
              }
              // Activity bundle — 2+ consecutive non-special tools.
              if (isActivityEntry(entry)) {
                return <ActivityRow key={`activity-${idx}`} tools={entry.tools} />;
              }
              const tool = entry;
              // Workflow runtime cards (WS-D)
              if (tool.toolName === 'workflow_preview') {
                return <WorkflowPreviewCard key={`wf-preview-${idx}`} preview={tool.input as never} />;
              }
              if (tool.toolName === 'workflow_started') {
                const wi = tool.input as { run_id?: string; name?: string };
                return wi.run_id ? <WorkflowRunCard key={`wf-run-${idx}`} runId={wi.run_id} name={wi.name} /> : null;
              }
              // CI watch + autofix cards (WS-J)
              if (tool.toolName === 'ci_started') {
                const ci = tool.input as { task_id?: string; branch?: string };
                return ci.task_id ? <CIStatusCard key={`ci-${idx}`} taskId={ci.task_id} branch={ci.branch} /> : null;
              }
              if (tool.toolName === 'ci_diagnosis') {
                return <CIDiagnosisCard key={`ci-dx-${idx}`} data={tool.input as never} />;
              }
              // Workspace prompt — render interactive folder picker card
              if (tool.toolName === 'ws_workspace_prompt') {
                const input = tool.input as {
                  reason?: string; suggested?: string;
                  recent?: string[]; tool_use_id?: string;
                };
                return (
                  <WorkspacePromptCard
                    key={`ws-prompt-${idx}`}
                    reason={input.reason ?? ''}
                    suggested={input.suggested ?? ''}
                    recent={input.recent ?? []}
                    toolUseId={input.tool_use_id ?? tool.toolId}
                  />
                );
              }

              // Default: collapsible tool trace
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
                  {tool.previewImage ? (
                    <div className="skill-trace-output">
                      <PreviewScreenshotCard
                        mediaType={tool.previewImage.media_type}
                        data={tool.previewImage.data}
                        caption={tool.result}
                      />
                    </div>
                  ) : tool.result && (
                    <div className="skill-trace-output">
                      <pre>{tool.result}</pre>
                    </div>
                  )}
                </details>
              );
            })}
          </div>
        )}

        {/* Standalone team cards: any report NOT anchored to a team_create or
         *  spawn_agent tool-use row above renders directly here. That covers
         *  background /subagent messages (no toolUse at all), question-round
         *  commits where the team trace landed on an earlier message, and
         *  stopped turns that carry only the partial report. */}
        {!isUser && message.teamReports &&
          Object.values(message.teamReports)
            .filter((r) => findMatchingTeamReports(
              (message.toolUse ?? []).filter(
                (t) => t.toolName === 'team_create' || t.toolName === 'spawn_agent',
              ),
              { [r.team_id]: r },
            ).length === 0)
            .map((r) => (
              <div className="skill-traces" key={`standalone-team-${r.team_id}`}>
                <TeamReportCard report={r} />
              </div>
            ))}

        {/* User question interactive card. The group component is adaptive: a
         *  single question renders as clean answer chips (answering submits
         *  immediately), 2+ stack into one form with a single submit. The
         *  legacy `userQuestion` path still renders old persisted messages. */}
        {!isUser && message.userQuestions && message.userQuestions.length > 0 ? (
          <>
            {/* Prose the model streamed before asking renders above the card. */}
            {message.content && <MarkdownRenderer content={message.content} stepFormat />}
            <UserQuestionGroupCard message={message} />
          </>
        ) : !isUser && message.userQuestion ? (
          <>
            {message.content && <MarkdownRenderer content={message.content} stepFormat />}
            <UserQuestionCard
              question={message.userQuestion.question}
              options={message.userQuestion.options}
              answered={message.userQuestion.answered}
              message={message}
            />
          </>
        ) : !isUser && message.plan ? (
          <>
            {message.content && <MarkdownRenderer content={message.content} stepFormat />}
            <PlanCard plan={message.plan} />
          </>
        ) : !isUser && message.programArtifact ? (
          <ProgramArtifactCard
            title={message.programArtifact.title}
            html={message.programArtifact.html}
            description={message.programArtifact.description}
          />
        ) : isUser ? (
          isEditing ? (
            <>
              <textarea
                ref={textareaRef}
                className="user-msg-edit-textarea"
                value={editText}
                onChange={(e) => setEditText(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); handleEditConfirm(); }
                  if (e.key === 'Escape') { e.preventDefault(); handleEditCancel(); }
                }}
                // rows={1} as the seed; the layout effect grows it from there.
                rows={1}
              />
              <div className="user-msg-edit-hint">
                <kbd>Enter</kbd> to save &middot;{' '}
                <kbd>Shift</kbd>+<kbd>Enter</kbd> for newline &middot;{' '}
                <kbd>Esc</kbd> to cancel
              </div>
              <div className="user-msg-edit-actions">
                <button className="edit-confirm" onClick={handleEditConfirm} title="Save & resend (Enter)" type="button">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <polyline points="20 6 9 17 4 12"/>
                  </svg>
                </button>
                <button onClick={handleEditCancel} title="Cancel (Esc)" type="button">
                  <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                    <line x1="18" y1="6" x2="6" y2="18"/><line x1="6" y1="6" x2="18" y2="18"/>
                  </svg>
                </button>
              </div>
            </>
          ) : (
            <>
              {/* Attached files — rendered inside the user bubble, above
               * the text, matching the affordance you get in ChatGPT /
               * Claude.ai. Read from `attachmentNames` which useChatStream
               * stamps onto the message at send time. Each chip is read-
               * only (already sent) and reveals the full filename on
               * hover so very long names stay readable. */}
              {message.attachmentNames && message.attachmentNames.length > 0 && (
                <div className="msg-attachments">
                  {message.attachmentNames.map((name, i) => (
                    <div key={`${name}-${i}`} className="msg-attachment-chip" title={name}>
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                        <path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/>
                        <polyline points="14 2 14 8 20 8"/>
                      </svg>
                      <span className="msg-attachment-name">{name}</span>
                    </div>
                  ))}
                </div>
              )}
              <div style={{ whiteSpace: 'pre-wrap' }}>{message.content}</div>
            </>
          )
        ) : message.content ? (
          <MarkdownRenderer content={message.content} stepFormat />
        ) : null}

        {/* Timestamp */}
        {message.timestamp && (
          <span className="msg-timestamp">{formatMessageTimestamp(message.timestamp)}</span>
        )}

        {/* Message actions — different for user vs assistant */}
        {isUser ? (
          <div className="msg-actions">
            <button className="msg-action-btn" onClick={() => void navigator.clipboard.writeText(message.content)} title="Copy">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
              </svg>
            </button>
            <button className="msg-action-btn" onClick={handleEdit} title="Edit">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <path d="M11 4H4a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h14a2 2 0 0 0 2-2v-7"/>
                <path d="M18.5 2.5a2.121 2.121 0 0 1 3 3L12 15l-4 1 1-4 9.5-9.5z"/>
              </svg>
            </button>
            <button className="msg-action-btn" onClick={handleRegenerate} title="Regenerate">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
            </button>
            <button className="msg-action-btn" onClick={handleDelete} title="Delete">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
          </div>
        ) : (
          <div className="msg-actions">
            <button className="msg-action-btn" onClick={() => void navigator.clipboard.writeText(message.content)} title="Copy">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
              </svg>
            </button>
            <button className="msg-action-btn" onClick={() => void copyRichText(message.content)} title="Copy Rich Text">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                <polyline points="15 14 17 16 21 12"/>
              </svg>
            </button>
            <button className="msg-action-btn" onClick={handleRegenerate} title="Regenerate">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
              </svg>
            </button>
            <button className="msg-action-btn" onClick={handleDelete} title="Delete">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
              </svg>
            </button>
            <button className="msg-action-btn" onClick={() => exportSingleMessage(message)} title="Export">
              <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
              </svg>
            </button>
          </div>
        )}
      </div>
    </div>
  );
};
