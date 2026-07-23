import React, { useEffect, useRef, useCallback, useState, useMemo } from 'react';
import { useSessionStore } from '@/stores/sessionStore';
import { useActiveChatStore, getActiveChatStore, getChatStore } from '@/stores/sessionRuntimes';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { getActiveTranscriptionStore } from '@/stores/sessionRuntimes';
import { ErrorBoundary } from '@/components/common/ErrorBoundary';
import { ChatMessage } from './ChatMessage';
import { computeTaskCheckpoints } from './TaskCard';
import { StreamingMessage } from './StreamingMessage';
import { ChatInput } from './ChatInput';
import { GoalBanner } from './GoalBanner';
import { ApprovalBanner } from './ApprovalBanner';
import { formatMessageTimestamp, formatSegmentTimestamp } from '@/utils/formatTimestamp';
import { useTaskStore, type SessionTask } from '@/stores/taskStore';
import { fetchSessionTasks } from '@/api/tasks';
import { permissionModeLabel } from '@/utils/permissionModes';

/**
 * Chat panel matching the vanilla #chatPanelWrap structure exactly.
 *
 * Structure:
 *   div.panel#chatPanelWrap  (adds .chat-panel-active when messages exist)
 *     div.panel-header#chatPanelHeader (hidden until chat active)
 *     div.panel-body.chat-messages#chatMessages
 *       div.chat-welcome-center#chatWelcomeCenter (shown when no messages)
 *     form.chat-input-area#chatForm
 */
export const ChatPanel: React.FC = () => {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const clearChat = useSessionStore((s) => s.clearChat);
  const branchSession = useSessionStore((s) => s.branchSession);
  const messages = useActiveChatStore((s) => s.messages);
  const isStreaming = useActiveChatStore((s) => s.isStreaming);
  const currentStreamContent = useActiveChatStore((s) => s.currentStreamContent);
  const permissionMode = useSettingsStore((s) => s.config.permissionMode ?? 'default');
  const wsConnected = useUIStore((s) => s.wsConnected);

  // Friendly mode label for the workspace mode indicator, shared with the
  // composer pill and status bar so every surface names modes identically.
  const modeLabel = permissionModeLabel(permissionMode);
  const messagesRef = useRef<HTMLDivElement>(null);
  const messagesEndRef = useRef<HTMLDivElement>(null);
  // Tracks whether the user is currently parked at the bottom of the
  // chat. Used to decide whether new messages should auto-scroll. Held
  // as a ref (not state) so the scroll handler can update it without
  // triggering re-renders on every wheel tick.
  const wasAtBottomRef = useRef(true);
  // Count of new messages that arrived while the user was NOT at the
  // bottom — surfaced as a "↓ N new" pill so live cron firings aren't
  // silently buried below the fold.
  const [unseenBelow, setUnseenBelow] = useState(0);

  // Use chatStore as the single source of truth for the current conversation.
  // Session history is loaded into chatStore when switching sessions.
  const allMessages = messages;
  const hasMessages = messages.length > 0 || isStreaming;

  // Tasks rows are deduped at the conversation level: a row appears only at a
  // turn where the cumulative task state changed (computeTaskCheckpoints), so
  // the same "N/M done" row no longer repeats every turn. When the live store
  // says this session has no tasks (authoritatively hydrated empty), suppress
  // all rows so an inline count can never disagree with an empty drawer.
  const sessionTasks = useTaskStore((s) => (currentSessionId ? s.tasksBySession[currentSessionId] : undefined));
  const taskCheckpoints = useMemo<Map<number, SessionTask[]>>(
    () => (sessionTasks !== undefined && sessionTasks.length === 0
      ? new Map<number, SessionTask[]>()
      : computeTaskCheckpoints(allMessages)),
    [allMessages, sessionTasks],
  );

  // Keys of messages whose committed bubble must NOT replay the msgIn entrance
  // animation, because their content already animated in while streaming (the
  // StreamingMessage bubble). finishStream() flips isStreaming false AND appends
  // the final assistant message in one store update, so at the stream→idle
  // transition (within the same session) the last message is exactly the one
  // that was streaming — mark its key. The set only grows within the component's
  // lifetime, so a suppressed message never un-suppresses (which would itself
  // restart the CSS animation). A fresh mount (session reload) starts empty, so
  // loaded history and the user's own messages keep their normal entrance.
  //
  // Derived during render via the sanctioned "store information from previous
  // renders" pattern (a guarded setState during render, which React resolves
  // before commit) so the class lands on the committed bubble's very first
  // paint — no one-frame blink.
  const [noEnterKeys, setNoEnterKeys] = useState<Set<string>>(() => new Set());
  const [streamCtx, setStreamCtx] = useState<{ sessionId: string | null; streaming: boolean }>(
    () => ({ sessionId: currentSessionId, streaming: isStreaming }),
  );
  if (streamCtx.sessionId !== currentSessionId || streamCtx.streaming !== isStreaming) {
    if (
      streamCtx.sessionId === currentSessionId &&
      streamCtx.streaming &&
      !isStreaming &&
      allMessages.length > 0
    ) {
      const lastIdx = allMessages.length - 1;
      const last = allMessages[lastIdx];
      const key = `${last.timestamp}-${lastIdx}`;
      if (last.role === 'assistant' && !noEnterKeys.has(key)) {
        setNoEnterKeys((prev) => {
          const next = new Set(prev);
          next.add(key);
          return next;
        });
      }
    }
    setStreamCtx({ sessionId: currentSessionId, streaming: isStreaming });
  }

  // Detect whether the scroll position is at (or near) the bottom.
  // Tolerance of 80 px so small layout shifts don't flip the flag, and
  // anything within ~one card height of the bottom still counts as
  // "user is following the conversation". Re-evaluated on every scroll
  // event of the panel-body.
  const recomputeAtBottom = useCallback(() => {
    const el = messagesRef.current;
    if (!el) return;
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    const atBottom = distance < 80;
    wasAtBottomRef.current = atBottom;
    if (atBottom) setUnseenBelow(0);
  }, []);

  useEffect(() => {
    const el = messagesRef.current;
    if (!el) return;
    el.addEventListener('scroll', recomputeAtBottom, { passive: true });
    return () => el.removeEventListener('scroll', recomputeAtBottom);
  }, [recomputeAtBottom]);

  // Hydrate the Tasks store for this session so the in-chat task row and the
  // Tasks drawer reflect the plan even on a restored conversation (the live
  // `todo_update` SSE events don't replay). The server list is authoritative
  // and is written even when empty, so a session with no (or cleared) tasks
  // resolves to [] instead of each message falling back to its own partial
  // per-turn snapshot. The one exception is a turn that is actively streaming:
  // its live `todo_update` is the fresher source, so a slow GET must not
  // clobber it. Fire-and-forget.
  useEffect(() => {
    if (!currentSessionId) return;
    let active = true;
    void fetchSessionTasks(currentSessionId)
      .then((tasks) => {
        if (!active) return;
        if (getChatStore(currentSessionId).getState().isStreaming) return;
        useTaskStore.getState().setTasks(currentSessionId, tasks);
      })
      .catch(() => {});
    return () => { active = false; };
  }, [currentSessionId]);

  // Smart auto-scroll: only follow new messages when the user is
  // already at the bottom. If they've scrolled up to read something,
  // increment the unseen counter instead of yanking the viewport.
  // Smooth scrolling for user-initiated turns, instant for background
  // cron firings to avoid stacking animations under rapid arrivals.
  const prevMessageCountRef = useRef(allMessages.length);
  useEffect(() => {
    const grew = allMessages.length > prevMessageCountRef.current;
    const delta = allMessages.length - prevMessageCountRef.current;
    prevMessageCountRef.current = allMessages.length;
    if (!grew) return;
    if (wasAtBottomRef.current) {
      const behavior: ScrollBehavior =
        currentStreamContent.length > 0 ? 'smooth' : 'auto';
      messagesEndRef.current?.scrollIntoView({ behavior, block: 'end' });
    } else {
      setUnseenBelow((n) => n + delta);
    }
  }, [allMessages.length, currentStreamContent]);

  // Stream tokens flow through currentStreamContent. When the user is
  // following along (at bottom), keep snapping the viewport to the
  // tail. Independent of the count-based effect so it fires per token.
  useEffect(() => {
    if (!currentStreamContent) return;
    if (!wasAtBottomRef.current) return;
    messagesEndRef.current?.scrollIntoView({ behavior: 'auto', block: 'end' });
  }, [currentStreamContent]);

  const jumpToBottom = useCallback(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth', block: 'end' });
    setUnseenBelow(0);
  }, []);

  const handleClear = useCallback(() => {
    getActiveChatStore().getState().clearMessages();
    clearChat();
  }, [clearChat]);

  // Fork the current conversation into a new session. Mirrors the Sidebar
  // right-click "Branch" action (branchSession copies persisted state via the
  // branch API, reloads the session list, then switches to the new session).
  const handleBranch = useCallback(() => {
    if (!currentSessionId) return;
    void branchSession(currentSessionId);
  }, [branchSession, currentSessionId]);

  const handleExport = useCallback(() => {
    const { segments, speakerNames } = getActiveTranscriptionStore().getState();
    let md = `# Conversation Export\n\n`;
    md += `**Session:** ${currentSessionId ?? 'N/A'}\n`;
    md += `**Exported:** ${new Date().toISOString()}\n\n---\n\n`;

    for (const msg of allMessages) {
      const time = formatMessageTimestamp(msg.timestamp);
      if (msg.role === 'user') {
        md += `## You (${time})\n\n${msg.content}\n\n`;
        if (msg.attachmentNames?.length) {
          md += `*Attachments: ${msg.attachmentNames.join(', ')}*\n\n`;
        }
      } else {
        md += `## Assistant (${time})\n\n`;
        if (msg._thinkingText) {
          md += `<details><summary>Thinking (${((msg._thinkingMs ?? 0) / 1000).toFixed(1)}s)</summary>\n\n${msg._thinkingText}\n\n</details>\n\n`;
        }
        if (msg.toolUse?.length) {
          md += `**Tools:** ${msg.toolUse.map(t => `${t.toolName} (${t.status})`).join(', ')}\n\n`;
        }
        if (msg.userQuestion) {
          md += `> **Question:** ${msg.userQuestion.question}\n`;
          md += `> **Options:** ${msg.userQuestion.options.join(', ')}\n`;
          if (msg.userQuestion.answered) md += `> *Answered*\n`;
          md += '\n';
        }
        if (msg.content) md += `${msg.content}\n\n`;
        if (msg._usage) {
          md += `*[${msg._usage.input_tokens.toLocaleString()} in / ${msg._usage.output_tokens.toLocaleString()} out]*\n\n`;
        }
      }
      md += `---\n\n`;
    }

    // Include transcription if segments exist
    if (segments.length > 0) {
      md += `\n# Transcription\n\n`;
      for (const seg of segments) {
        const speaker = speakerNames[seg.speaker] ?? seg.speaker;
        const time = formatSegmentTimestamp(seg.timestamp);
        md += `**[${time}] ${speaker}:** ${seg.text}\n\n`;
      }
    }

    const blob = new Blob([md], { type: 'text/markdown' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `conversation-${currentSessionId ?? 'export'}.md`;
    a.click();
    URL.revokeObjectURL(url);
  }, [allMessages, currentSessionId]);

  return (
    <div className={`panel${hasMessages ? ' chat-panel-active' : ''}`} id="chatPanelWrap">
      {/* Panel header — hidden until chat is active */}
      <div
        className="panel-header"
        id="chatPanelHeader"
        style={{ display: hasMessages ? undefined : 'none' }}
      >
        <h2>
          <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <path d="M21 15a2 2 0 0 1-2 2H7l-4 4V5a2 2 0 0 1 2-2h14a2 2 0 0 1 2 2z"/>
          </svg>
          Assistant
        </h2>
        {/* Workspace mode indicator — shown when workspace connected.
         * Reads the actual permission mode (default / auto / plan /
         * acceptEdits / bypassPermissions / dontAsk). The old code hard-
         * coded a binary plan/auto split which made every non-plan mode
         * misreport as "Auto Mode". */}
        {wsConnected && (
          <div className="ws-mode-indicator" id="wsModeIndicator" title={`Permission mode: ${modeLabel}`}>
            {modeLabel}
          </div>
        )}
        <div className="panel-header-actions">
          <button className="btn btn-sm" id="chatDownloadBtn" onClick={handleExport} disabled={!hasMessages}>
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
            </svg>
            Export
          </button>
          <button className="btn btn-sm" id="chatBranchBtn" onClick={handleBranch} disabled={!hasMessages || !currentSessionId} title="Fork conversation into a new session">
            <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <line x1="6" y1="3" x2="6" y2="15"/><circle cx="18" cy="6" r="3"/><circle cx="6" cy="18" r="3"/><path d="M18 9a9 9 0 0 1-9 9"/>
            </svg>
            Branch
          </button>
          <button className="btn btn-sm" id="chatClearBtn" onClick={handleClear}>Clear</button>
          {wsConnected && (
            <button
              className="btn btn-sm"
              id="resetApprovalsBtn"
              title="Reset session approval rules"
              onClick={() => {
                getActiveChatStore().getState().resetSessionApprovals();
                useUIStore.getState().addToast({ type: 'info', message: 'Session approval rules reset', duration: 2000 });
              }}
            >
              Reset Approvals
            </button>
          )}
        </div>
      </div>

      {/* Chat messages area */}
      <div className="panel-body chat-messages" id="chatMessages" ref={messagesRef}>
        {/* Welcome centered state */}
        {!hasMessages && (
          <div className="chat-welcome-center" id="chatWelcomeCenter">
            <div className="welcome-logo">
              <svg width="48" height="48" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="1.5" strokeLinecap="round">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                <line x1="12" y1="19" x2="12" y2="23"/>
                <line x1="8" y1="23" x2="16" y2="23"/>
              </svg>
            </div>
            {/* The header already reads "Whisper Studio", so the welcome
             *  heading does real work: an action-oriented prompt + a one-line
             *  subtitle that says what the app is, rather than repeating the
             *  product name. */}
            <h2 className="welcome-title">What can I help with?</h2>
            <p className="welcome-subtitle">Transcribe a meeting, chat with your code, or run a skill.</p>
            {/* Teaching starter actions — each names one of the product's
             *  pillars and triggers the real surface, so a first-run user
             *  learns the app in three clicks. */}
            <div className="welcome-actions">
              <button
                type="button"
                className="welcome-action"
                onClick={() => window.dispatchEvent(new CustomEvent('whisper-start-recording'))}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                  <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
                </svg>
                <strong>Transcribe a meeting</strong>
                <span>Live transcription starts instantly. 100% local, nothing leaves your machine</span>
              </button>
              <button
                type="button"
                className="welcome-action"
                onClick={() => useUIStore.getState().openWorkspaceConnect()}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
                </svg>
                <strong>Connect a workspace</strong>
                <span>Chat with your code and files</span>
              </button>
              <button
                type="button"
                className="welcome-action"
                onClick={() => useUIStore.getState().openSettings('skills')}
              >
                <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <path d="M12 2l2.4 5.3L20 8.5l-4 4.1.9 5.9L12 15.8l-4.9 2.7.9-5.9-4-4.1 5.6-1.2z"/>
                </svg>
                <strong>Explore skills</strong>
                <span>Slash commands and automations</span>
              </button>
            </div>
          </div>
        )}

        {/* Rendered messages */}
        {allMessages.map((msg, idx) => {
          const key = `${msg.timestamp}-${idx}`;
          return (
            <ChatMessage
              key={key}
              message={msg}
              index={idx}
              taskCheckpoint={taskCheckpoints.get(idx) ?? null}
              noEnter={noEnterKeys.has(key)}
            />
          );
        })}

        {/* Streaming response */}
        {isStreaming && (
          <StreamingMessage content={currentStreamContent} isStreaming={isStreaming} />
        )}

        {/* Approval card — shown when a tool needs permission */}
        <ErrorBoundary label="ApprovalBanner">
          <ApprovalBanner />
        </ErrorBoundary>

        <div ref={messagesEndRef} />

        {/* Floating "new messages" pill — appears when live cron firings
         * (or other background events) arrive while the user is scrolled
         * up reading older content. Click to snap to the bottom. */}
        {unseenBelow > 0 && (
          <button
            type="button"
            className="chat-unseen-pill"
            onClick={jumpToBottom}
            aria-label={`Jump to ${unseenBelow} new message${unseenBelow === 1 ? '' : 's'}`}
          >
            ↓ {unseenBelow} new
          </button>
        )}
      </div>

      {/* Goal banner + chat input form */}
      <GoalBanner sessionId={currentSessionId} />
      <ChatInput sessionId={currentSessionId} />
    </div>
  );
};
