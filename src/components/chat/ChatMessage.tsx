import React, { useState, useCallback, useRef, useLayoutEffect } from 'react';
import type { ChatMessage as ChatMessageType } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { TaskReportCard } from './TaskReportCard';
import { AgentReportCard } from './AgentReportCard';
import { CronEventCard } from '@/components/chat/CronEventCard';
import { SessionMessageCard } from '@/components/chat/SessionMessageCard';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import { formatMessageTimestamp } from '@/utils/formatTimestamp';
import { extractFlatCronPayload } from '@/components/chat/messageActions';

export interface ChatMessageProps {
  message: ChatMessageType;
  index: number;
}

/**
 * Renders every row that is not part of an assistant turn: the user bubble
 * and the card roles. Assistant rows render as one bubble per turn
 * (TurnBubble).
 *
 * Structure:
 *   div.chat-msg-wrap.user-wrap
 *     div.chat-msg.user
 *       (content)
 */
const ChatMessageView: React.FC<ChatMessageProps> = ({ message, index }) => {
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
    window.dispatchEvent(new CustomEvent('whisper-regenerate', {
      detail: {
        index,
        content: message.content,
        attachmentIds: message.attachmentIds,
        attachmentNames: message.attachmentNames,
      },
    }));
  }, [index, message.content, message.attachmentIds, message.attachmentNames]);

  // A user message deletes from here: the turn it started goes with it.
  const handleDelete = useCallback(() => {
    getActiveChatStore().getState().deleteMessagesFrom(index);
  }, [index]);

  // cron_event rows render as a standalone card: none of the user bubble's
  // chrome (edit, regenerate, copy, attachments) applies. Hooks above are
  // still called on every render so React's rules-of-hooks stay satisfied.
  // The payload was delivered either live via SSE (useChatStream ->
  // addMessage) or by session resume (chat_history JSON).
  //
  // Tolerate two shapes for back-compat with already-persisted rows:
  //   - nested:  { role: 'cron_event', cronEvent: {event_type, ...} }
  //   - flat:    { role: 'cron_event', event_type, cron_id, cron_name, ... }
  // Newly-emitted events use the nested shape; the flat fallback rescues
  // rows written before that fix landed. A row with neither still shows
  // its text.
  if (message.role === 'cron_event') {
    const payload = message.cronEvent ?? extractFlatCronPayload(message);
    if (payload) return <CronEventCard event={payload} />;
    if (!message.content) return null;
    return (
      <div className="chat-msg-wrap assistant-wrap">
        <div className="chat-msg assistant">
          <MarkdownRenderer content={message.content} />
        </div>
      </div>
    );
  }

  // The wake turn's answer to agent reports that landed with no live turn:
  // the assistant speaking, so it gets the assistant bubble, with a small
  // label saying why it appeared without a prompt.
  if (message.role === 'agent_answer') {
    const text = message.agentAnswer?.text ?? '';
    if (!text) return null;
    return (
      <div className="chat-msg-wrap assistant-wrap">
        <div className="chat-msg assistant agent-answer">
          <div className="agent-answer-label">
            Answered from the agents' reports{message.agentAnswer?.team_name ? ` (${message.agentAnswer.team_name})` : ''}
          </div>
          <MarkdownRenderer content={text} />
        </div>
      </div>
    );
  }

  // Agent reports delivered outside a live turn (cancelled team, background
  // or resumed agent). UI-only in storage; the model reads a relabeled copy.
  // Left-aligned at an assistant turn's width, but not a bubble: no message
  // actions, and the timestamp only when the payload carries one. A report
  // with no agents has nothing to show, so it draws nothing at all.
  if (message.role === 'agent_report') {
    const report = message.agentReport;
    if (!report?.agents?.length) return null;
    return (
      <div className="chat-msg-wrap assistant-wrap agent-report-wrap">
        <AgentReportCard report={report} />
        {report.timestamp && <span className="msg-timestamp">{formatMessageTimestamp(report.timestamp)}</span>}
      </div>
    );
  }

  // The result of a background command this chat started, which finished
  // after its turn ended. UI-only in storage; the model reads a relabeled
  // copy (server/tasks/wake.py). Drawn like an agent report.
  if (message.role === 'task_report') {
    const report = message.taskReport;
    if (!report) return null;
    return (
      <div className="chat-msg-wrap assistant-wrap agent-report-wrap">
        <TaskReportCard report={report} />
        {report.timestamp && <span className="msg-timestamp">{formatMessageTimestamp(report.timestamp)}</span>}
      </div>
    );
  }

  // Cross-session message rows: a text message another session sent this
  // one via send_session_message. Same UI-only-in-storage contract as
  // cron events (the backend relabels it as a user turn before the
  // model sees it, see visible_chat_history).
  if (message.role === 'session_message') {
    if (message.sessionMessage) {
      return <SessionMessageCard message={message.sessionMessage} />;
    }
    return null;
  }

  if (message.role !== 'user') return null;

  return (
    <div className="chat-msg-wrap user-wrap">
      <div className={`chat-msg user${isEditing ? ' editing' : ''}`}>
        {isEditing ? (
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
            {/* Attached files, rendered inside the user bubble above the
             * text, matching the affordance you get in ChatGPT /
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
        )}

        {message.timestamp && (
          <span className="msg-timestamp">
            {message.spoken && (
              <span className="msg-spoken" title="Spoken in voice mode">
                <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M19 11a7 7 0 0 1-14 0"/><line x1="12" y1="18" x2="12" y2="22"/></svg>
                spoken ·{' '}
              </span>
            )}
            {formatMessageTimestamp(message.timestamp)}
          </span>
        )}

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
      </div>
    </div>
  );
};

/** Memoized: the panel re-renders for its own state (the new-messages pill,
 *  a turn starting or ending), and a long session must not re-render every
 *  message each time. Messages are immutable store objects, so identity is
 *  the right comparison. */
export const ChatMessage = React.memo(ChatMessageView);
