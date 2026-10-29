import React, { useEffect, useRef, useCallback, useState, useMemo } from 'react';
import { useSessionStore } from '@/stores/sessionStore';
import { useActiveChatStore, getActiveChatStore, getChatStore } from '@/stores/sessionRuntimes';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { getActiveTranscriptionStore } from '@/stores/sessionRuntimes';
import { ErrorBoundary } from '@/components/common/ErrorBoundary';
import { ChatMessage } from './ChatMessage';
import { TurnBubble } from './TurnBubble';
import { isLiveTurn, turnEntries, turnKey, spokenTurn } from './turnEntries';
import { resolveTaskCheckpoints } from './TaskCard';
import { StreamingMessage } from './StreamingMessage';
import { ICON } from './activityIcons';
import { VoiceLiveMessage } from './VoiceLiveMessage';
import { ChatInput } from './ChatInput';
import { GoalBanner } from './GoalBanner';
import { ApprovalBanner } from './ApprovalBanner';
import { AutoModeBreakerBanner } from './AutoModeBreakerBanner';
import { formatMessageTimestamp, formatSegmentTimestamp } from '@/utils/formatTimestamp';
import { downloadFile } from '@/utils/downloadFile';
import { useTaskStore } from '@/stores/taskStore';
import { fetchSessionTasks } from '@/api/tasks';
import { permissionModeLabel } from '@/utils/permissionModes';
import { useFollowBottom } from '@/hooks/useFollowBottom';
import type { Delivery } from '@/types/chat';

/**
 * The streaming reply. The only subscriber to the streamed text, so a token
 * re-renders this reply and not the panel with every message above it. A
 * turn that has not committed a row yet gets a bubble of its own; once it
 * has, the reply is the embedded tail of the turn's bubble.
 */
const LiveReply: React.FC<{
  embedded?: boolean;
  priorDeliveries?: readonly Delivery[];
}> = ({ embedded, priorDeliveries }) => {
  const content = useActiveChatStore((s) => s.currentStreamContent);
  return <StreamingMessage content={content} isStreaming embedded={embedded} priorDeliveries={priorDeliveries} />;
};

/**
 * Messages the user sent into the running turn that the model has not read
 * yet, below the live reply. The stream moves each into the transcript where
 * the model reads it (the midturn_read frame), and the answer to it streams
 * below it as a reply of its own.
 */
const PendingNotes: React.FC = () => {
  const pending = useActiveChatStore((s) => s.pendingMidTurn);
  return (
    <>
      {pending.map((note) => (
        <div key={note.timestamp} className="chat-msg-wrap user-wrap pending-note">
          <div className="chat-msg user">
            <div style={{ whiteSpace: 'pre-wrap' }}>{note.content}</div>
            <span
              className="msg-timestamp"
              title="The assistant reads this when it finishes its current step, then answers it below."
            >
              Queued
            </span>
          </div>
        </div>
      ))}
    </>
  );
};

/**
 * Chat panel matching the vanilla #chatPanelWrap structure exactly.
 *
 * Structure:
 *   div.panel#chatPanelWrap  (adds .chat-panel-active when messages exist)
 *     div.panel-header#chatPanelHeader (hidden until chat active)
 *     div.panel-body.chat-messages#chatMessages
 *       div.chat-welcome-center#chatWelcomeCenter (shown when no messages)
 *     form.chat-input-area#chatForm (ChatInput)
 *     div.welcome-actions (no messages: the tiles sit under the composer,
 *       on its width)
 */
export const ChatPanel: React.FC = () => {
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const clearChat = useSessionStore((s) => s.clearChat);
  const branchSession = useSessionStore((s) => s.branchSession);
  const messages = useActiveChatStore((s) => s.messages);
  const isStreaming = useActiveChatStore((s) => s.isStreaming);
  const permissionMode = useSettingsStore((s) => s.config.permissionMode ?? 'default');
  const wsConnected = useUIStore((s) => s.wsConnected);

  // Friendly mode label for the workspace mode indicator, shared with the
  // composer pill and status bar so every surface names modes identically.
  const modeLabel = permissionModeLabel(permissionMode);
  const messagesRef = useRef<HTMLDivElement>(null);
  // Count of new bubbles that appeared while the user was reading further
  // up, surfaced as a "N new" pill so live cron firings aren't silently
  // buried below the fold.
  const [unseenBelow, setUnseenBelow] = useState(0);
  const clearUnseen = useCallback(() => setUnseenBelow(0), []);
  // The view follows the newest line until the user scrolls up, and stays
  // where they left it until they scroll back down: the transcript's rule.
  const follow = useFollowBottom(messagesRef, currentSessionId, clearUnseen);

  // Use chatStore as the single source of truth for the current conversation.
  // Session history is loaded into chatStore when switching sessions.
  const allMessages = messages;
  const hasMessages = messages.length > 0 || isStreaming;
  // One bubble per assistant turn: a turn streams as several store rows
  // (prose before a tool call, every card), which render together. Indices
  // stay real so actions and checkpoints still address the right store rows.
  const displayEntries = useMemo(() => turnEntries(allMessages), [allMessages]);
  const lastEntry = displayEntries[displayEntries.length - 1];
  // The turn being written: a stream whose turn already committed a row
  // draws its live tail inside that turn's bubble. A stream that starts a new
  // turn (no assistant row after the last other row yet), or whose last row
  // is a slash command's (standalone), has its own bubble.
  const liveTurn = isStreaming && isLiveTurn(lastEntry) ? lastEntry : null;

  // Tasks rows are deduped at the conversation level: a row appears only at a
  // turn where the cumulative task state changed (computeTaskCheckpoints), so
  // the same "N/M done" row no longer repeats every turn. When the live store
  // says this session has no tasks (authoritatively hydrated empty), suppress
  // all rows so an inline count can never disagree with an empty drawer.
  const sessionTasks = useTaskStore((s) => (currentSessionId ? s.tasksBySession[currentSessionId] : undefined));
  // Bind the most-recent (current) Tasks row to the authoritative task store
  // (the same single source the Tasks panel reads) so the inline chip's count
  // and "all done" always match the panel and can never freeze at a stale
  // "N/M in progress". Earlier checkpoints stay historical. `liveCheckpointIdx`
  // is the message whose row renders live (-1 while streaming, when the live
  // StreamingMessage row is current instead). See resolveTaskCheckpoints.
  const { checkpoints: taskCheckpoints, liveIdx: liveCheckpointIdx } = useMemo(
    () => resolveTaskCheckpoints(allMessages, sessionTasks, isStreaming),
    [allMessages, sessionTasks, isStreaming],
  );

  // Keys of turns whose bubble must NOT replay the msgIn entrance animation,
  // because the turn already animated in as the live reply. A turn streams as
  // several store rows: its first commit (a segment flush, a card) swaps the
  // live bubble for the turn's TurnBubble, and its last row lands at the
  // stream-to-idle flip (finishStream appends it and flips isStreaming in one
  // store update). A row committed while this session streams belongs to a
  // turn at the end of the list, so every turn that holds a row added by such
  // a commit is marked (one React batch can carry several commits). The set
  // only grows within the component's lifetime, so a
  // suppressed turn never un-suppresses (which would itself restart the CSS
  // animation). A fresh mount (session reload) starts empty, so loaded
  // history and the user's own messages keep their normal entrance.
  //
  // Derived during render via the sanctioned "store information from previous
  // renders" pattern (a guarded setState during render, which React resolves
  // before commit) so the class lands on the bubble's very first paint: no
  // one-frame blink.
  const [noEnterKeys, setNoEnterKeys] = useState<Set<string>>(() => new Set());
  const [streamCtx, setStreamCtx] = useState<{ sessionId: string | null; streaming: boolean; count: number }>(
    () => ({ sessionId: currentSessionId, streaming: isStreaming, count: allMessages.length }),
  );
  if (
    streamCtx.sessionId !== currentSessionId ||
    streamCtx.streaming !== isStreaming ||
    streamCtx.count !== allMessages.length
  ) {
    if (
      streamCtx.sessionId === currentSessionId &&
      streamCtx.streaming &&
      allMessages.length > streamCtx.count
    ) {
      const fresh: string[] = [];
      for (let e = displayEntries.length - 1; e >= 0; e -= 1) {
        const entry = displayEntries[e];
        const lastIndex = entry.kind === 'turn' ? entry.indices[entry.indices.length - 1] : entry.index;
        if (lastIndex < streamCtx.count) break;
        if (entry.kind === 'turn' && !noEnterKeys.has(turnKey(entry))) fresh.push(turnKey(entry));
      }
      if (fresh.length > 0) {
        setNoEnterKeys((prev) => new Set([...prev, ...fresh]));
      }
    }
    setStreamCtx({ sessionId: currentSessionId, streaming: isStreaming, count: allMessages.length });
  }

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

  // New bubbles that land while the user reads further up count toward the
  // pill instead of moving the view. The counter counts bubbles: a row that
  // only grows the last turn's bubble adds nothing to it.
  const prevMessageCountRef = useRef(allMessages.length);
  const prevEntryCountRef = useRef(displayEntries.length);
  useEffect(() => {
    const grew = allMessages.length > prevMessageCountRef.current;
    const delta = Math.max(0, displayEntries.length - prevEntryCountRef.current);
    prevMessageCountRef.current = allMessages.length;
    prevEntryCountRef.current = displayEntries.length;
    if (grew && delta > 0 && !follow.following()) {
      setUnseenBelow((n) => n + delta);
    }
  }, [allMessages.length, displayEntries.length, follow]);

  // The live tail a streaming turn's bubble draws after its committed rows.
  // Stable, so the bubble re-renders only when its rows change, never on a
  // token: LiveReply alone redraws the stream.
  const renderLive = useCallback(
    (priorDeliveries: readonly Delivery[]) => <LiveReply embedded priorDeliveries={priorDeliveries} />,
    [],
  );

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

    downloadFile(md, `conversation-${currentSessionId ?? 'export'}.md`, 'text/markdown');
  }, [allMessages, currentSessionId]);

  return (
    <div className={`panel${hasMessages ? ' chat-panel-active' : ''}`} id="chatPanelWrap">
      {/* Panel header, hidden until chat is active */}
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
        {/* Workspace mode indicator, shown when workspace connected.
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
                useUIStore.getState().addToast({ type: 'info', message: 'Session approval rules reset', duration: 2000, persist: false });
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
              <svg width="28" height="28" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round">
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
          </div>
        )}

        {/* Rendered messages: one bubble per assistant turn */}
        {displayEntries.map((entry) => {
          if (entry.kind === 'message') {
            return (
              <ChatMessage
                key={`${entry.message.timestamp}-${entry.index}`}
                message={entry.message}
                index={entry.index}
              />
            );
          }
          const key = turnKey(entry);
          return (
            <TurnBubble
              key={key}
              rows={entry.rows}
              indices={entry.indices}
              taskCheckpoints={taskCheckpoints}
              liveCheckpointIdx={liveCheckpointIdx}
              // A spoken turn replaces the live voice bubble that already
              // showed its words; replaying the entrance animation would read
              // as the message vanishing and streaming back in.
              noEnter={noEnterKeys.has(key) || spokenTurn(entry.rows)}
              renderLive={entry === liveTurn ? renderLive : undefined}
            />
          );
        })}

        {/* Voice mode: the utterance being spoken right now */}
        <VoiceLiveMessage />

        {/* Streaming response that starts a new turn */}
        {isStreaming && !liveTurn && <LiveReply />}

        {/* Messages sent into the running turn, until the model reads them */}
        <PendingNotes />

        {/* Approval card, shown when a tool needs permission */}
        <ErrorBoundary label="ApprovalBanner">
          <ApprovalBanner />
        </ErrorBoundary>

        {/* Auto-mode circuit breaker, shown after repeated confirmations */}
        <ErrorBoundary label="AutoModeBreakerBanner">
          <AutoModeBreakerBanner />
        </ErrorBoundary>

        {/* Floating "new messages" pill: appears when live cron firings
         * (or other background events) arrive while the user is scrolled
         * up reading older content. Click to snap to the bottom. */}
        {unseenBelow > 0 && (
          <button
            type="button"
            className="chat-unseen-pill"
            onClick={follow.jumpToBottom}
            aria-label={`Jump to ${unseenBelow} new message${unseenBelow === 1 ? '' : 's'}`}
          >
            {ICON.arrowDown}
            {unseenBelow} new
          </button>
        )}
      </div>

      {/* Goal banner + chat input form */}
      <GoalBanner sessionId={currentSessionId} />
      <ChatInput sessionId={currentSessionId} />
      {/* Teaching starter actions under the composer, on its width: each
       *  names one of the product's pillars and triggers the real surface,
       *  so a first-run user learns the app in three clicks. */}
      {!hasMessages && (
        <div className="welcome-actions">
          <button
            type="button"
            className="welcome-action"
            onClick={() => window.dispatchEvent(new CustomEvent('whisper-start-recording'))}
          >
            <span className="welcome-action-icon" aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                <path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/>
                <path d="M19 10v2a7 7 0 0 1-14 0v-2"/>
              </svg>
            </span>
            <strong>Transcribe</strong>
            <span>Live, on your Mac</span>
          </button>
          <button
            type="button"
            className="welcome-action"
            onClick={() => useUIStore.getState().openWorkspaceConnect()}
          >
            <span className="welcome-action-icon" aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/>
              </svg>
            </span>
            <strong>Workspace</strong>
            <span>Chat with your code</span>
          </button>
          <button
            type="button"
            className="welcome-action"
            onClick={() => useUIStore.getState().openSettings('skills')}
          >
            <span className="welcome-action-icon" aria-hidden="true">
              <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <path d="M13 2L3 14h9l-1 8 10-12h-9l1-8z"/>
              </svg>
            </span>
            <strong>Skills</strong>
            <span>Commands and automations</span>
          </button>
        </div>
      )}
    </div>
  );
};
