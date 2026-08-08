import React from 'react';
import type { SessionMessagePayload } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { formatMessageTimestamp } from '@/utils/formatTimestamp';
import { useSessionStore } from '@/stores/sessionStore';

interface SessionMessageCardProps {
  message: SessionMessagePayload;
}

/**
 * Inline rendering for a `role: 'session_message'` ChatMessage: a text
 * message another chat session sent this one via send_session_message.
 *
 * Same contract as CronEventCard/BackgroundTaskCard — the live SSE event and
 * the persisted chat_history row carry the same payload, so live-render and
 * replay-on-resume converge here. UI-only in storage; the backend relabels
 * it as a user turn before it reaches the model (see
 * visible_chat_history/_session_message_prompt_view), so this card can stay
 * plain without affecting what Claude sees.
 */
export const SessionMessageCard: React.FC<SessionMessageCardProps> = ({ message }) => {
  const when = formatMessageTimestamp(message.timestamp);
  const jumpToSession = useSessionStore((s) => s.sessions.some((sess) => sess.id === message.from_session_id))
    ? () => void useSessionStore.getState().switchSession(message.from_session_id)
    : null;

  return (
    <div className="session-message">
      <div className="session-message-summary">
        <span className="session-message-icon">&#9993;</span>
        <span className="session-message-title">
          Message from <strong>{message.from_title}</strong>
        </span>
        {jumpToSession && (
          <button className="session-message-jump" onClick={jumpToSession} title="Open that session">
            Jump to session
          </button>
        )}
        <span className="session-message-meta">{when}</span>
      </div>
      <div className="session-message-body">
        <MarkdownRenderer content={message.content} />
      </div>
    </div>
  );
};
