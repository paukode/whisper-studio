import React, { useState, useRef, useCallback, useEffect, useMemo } from 'react';
import { useActiveChatStore, getActiveChatStore } from '@/stores/sessionRuntimes';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { useChatStream } from '@/hooks/useChatStream';
import { updatePermissions } from '@/api/settings';
import { useChatAutocomplete } from '@/hooks/useChatAutocomplete';
import { useSlashCommands } from '@/hooks/useSlashCommands';
import { useComposerDragDrop } from '@/hooks/useComposerDragDrop';
import { useComposerChatEvents } from '@/hooks/useComposerChatEvents';
import { useDictationInput } from '@/hooks/useDictationInput';
import { useComposerAttachments } from '@/hooks/useComposerAttachments';
import { requestModelChange } from './dataRetentionConsent';
import { post, put } from '@/api/client';
import { useRecentWorkspaces } from '@/hooks/useRecentWorkspaces';
import { useQuery } from '@tanstack/react-query';
import { listIndexes, type IndexInfo } from '@/api/workspace';
import { useIndexSearchStore } from '@/stores/indexSearchStore';
import { toError } from '@/utils/toError';
import { EffortPicker } from './EffortPicker';
import { ResponseLengthPicker } from './ResponseLengthPicker';
import { LocalToggles } from './LocalToggles';
import { LocalContextWindowSlider } from './LocalContextWindowSlider';
import { ModeDropdown } from './ModeDropdown';
import { ModelDropdown } from './ModelDropdown';
import { WorkspaceDropdown } from './WorkspaceDropdown';
import {
  BASE_SLASH_COMMANDS,
  parseSkillMention,
  SUPPORTED_ATTACHMENT_SUMMARY,
  VOICE_SUBMIT_TRIGGERS,
  type SlashCommand,
} from './chatInputConstants';
import { TokenCounter } from './TokenCounter';
import { MoreMenu, type MoreSection } from './MoreMenu';

export interface ChatInputProps {
  sessionId: string | null;
}

/**
 * Chat input form with slash-command and @-mention autocomplete
 * matching the vanilla skill-autocomplete CSS classes.
 */
export const ChatInput: React.FC<ChatInputProps> = ({ sessionId }) => {
  const [text, setText] = useState('');
  const {
    attachments,
    setAttachments,
    attachmentsRef,
    uploadFilesAsChips,
    attachWorkspaceFileAsChip,
    uploadWorkspaceFile,
    removeAttachment,
    waitForUploads,
  } = useComposerAttachments();
  const [modeOpen, setModeOpen] = useState(false);
  const [modelOpen, setModelOpen] = useState(false);
  const [wsDropdownOpen, setWsDropdownOpen] = useState(false);
  // "+ More" overflow popover and which section inside it is expanded.
  const [moreOpen, setMoreOpen] = useState(false);
  const [moreSection, setMoreSection] = useState<MoreSection>(null);

  const textareaRef = useRef<HTMLTextAreaElement>(null);
  const acRef = useRef<HTMLDivElement>(null);

  const chatStream = useChatStream();
  const { mic, handleMicClick, inputTextRef, submitRef, stopMic } = useDictationInput({
    text,
    setText,
    textareaRef,
    sessionId,
  });
  const {
    fileInputRef,
    isDragOver,
    handleFileSelect,
    handleDragOver,
    handleDragLeave,
    handleDrop,
  } = useComposerDragDrop(uploadFilesAsChips);
  useComposerChatEvents(chatStream);
  // Long-lived SSE for out-of-band session notifications (cron firings
  // in particular). Stays connected for the lifetime of the chat view
  // so background events appear inline without a page refresh.

  /* ── Workspace dropdown helpers ── */
  const recentPaths = useRecentWorkspaces(wsDropdownOpen).slice(0, 5);

  const connectToWorkspace = useCallback(async (wsPath: string) => {
    try {
      // Backend returns {path, entries, writable}. `writable` is a soft
      // hint from os.access — when it's false we surface an info toast
      // so the user gets early warning that writes may fail, without
      // blocking the connection (os.access lies on network mounts /
      // root / macOS extended ACLs, so we never refuse based on it).
      const data = await post<{ writable?: boolean }>('/api/workspace/connect', { path: wsPath });
      useUIStore.getState().setWsConnected(true, wsPath);
      setWsDropdownOpen(false);
      const name = wsPath.split('/').pop();
      if (data?.writable === false) {
        useUIStore.getState().addToast({
          type: 'info',
          message: `Connected to ${name}. Folder may be read-only. Writes will be confirmed on first attempt.`,
          duration: 5000,
        });
      } else {
        useUIStore.getState().addToast({
          type: 'success',
          message: `Connected to ${name}`,
          duration: 2000,
        });
      }
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message || 'Connection failed' });
    }
  }, []);

  // Close all toolbar dropdowns on click outside.
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      const target = e.target as HTMLElement;
      if (!target.closest('.toolbar-dropdown-wrap')) {
        setWsDropdownOpen(false);
        setModeOpen(false);
        setModelOpen(false);
        setMoreOpen(false);
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, []);

  // Close toolbar dropdowns on Escape (matches EffortPicker's own dismiss).
  // Acts — and consumes the key via preventDefault — only when a dropdown is
  // actually open, so an idle ESC still reaches global shortcuts (the stream
  // kill switch). Open flags are deps so the handler sees current state.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key !== 'Escape') return;
      if (!(wsDropdownOpen || modeOpen || modelOpen || moreOpen)) return;
      e.preventDefault();
      setWsDropdownOpen(false);
      setModeOpen(false);
      setModelOpen(false);
      setMoreOpen(false);
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [wsDropdownOpen, modeOpen, modelOpen, moreOpen]);

  // Approve a saved plan: read its markdown, leave plan mode so writes are
  // allowed, and send an implementation turn. The full plan goes to the model;
  // the chat bubble stays short via displayText. Shared by the PlanCard button
  // and the "plan approved" phrase. planId omitted → the session's latest plan.
  const approvePlan = useCallback(async (planId?: string) => {
    try {
      let id = planId;
      if (!id) {
        const list = await fetch(`/api/plans?session=${encodeURIComponent(sessionId ?? '')}`)
          .then((r) => (r.ok ? r.json() : { plans: [] }))
          .catch(() => ({ plans: [] }));
        id = list.plans?.[0]?.id as string | undefined;
      }
      if (!id) {
        useUIStore.getState().addToast({ type: 'info', message: 'No plan to approve yet.' });
        return;
      }
      const md = await fetch(`/api/plans/${encodeURIComponent(id)}`).then((r) => (r.ok ? r.text() : null));
      if (md == null) {
        useUIStore.getState().addToast({ type: 'error', message: 'Could not load the plan to implement.' });
        return;
      }
      const s = useSettingsStore.getState();
      if (s.planMode || s.config.permissionMode === 'plan') {
        await updatePermissions({ mode: 'auto' }).catch(() => {});
        useSettingsStore.setState((st) => ({ planMode: false, config: { ...st.config, permissionMode: 'auto' } }));
      }
      await chatStream.send(
        `The plan below is approved. Implement it now, step by step, and verify each step as you go.\n\n---\n\n${md}`,
        { displayText: 'Approved — implement the plan.' },
      );
    } catch {
      useUIStore.getState().addToast({ type: 'error', message: 'Could not start the plan implementation.' });
    }
  }, [chatStream, sessionId]);

  // Approve button on a PlanCard dispatches this.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { planId?: string } | undefined;
      void approvePlan(detail?.planId);
    };
    window.addEventListener('whisper-approve-plan', handler);
    return () => window.removeEventListener('whisper-approve-plan', handler);
  }, [approvePlan]);

  // Listen for transcript right-click → "Quote in chat input" so a transcript
  // selection can be dropped into the composer without auto-sending. The
  // event name matches CHAT_INSERT_EVENT in TranscriptionPanel.tsx.
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent<{ text?: string }>).detail;
      const insert = detail?.text;
      if (!insert) return;
      setText((prev) => (prev ? prev + (prev.endsWith('\n') ? '' : '\n') + insert : insert));
      // Bring focus to the composer so the user can immediately keep typing.
      requestAnimationFrame(() => {
        textareaRef.current?.focus();
        const el = textareaRef.current;
        if (el) {
          const len = el.value.length;
          el.setSelectionRange(len, len);
        }
      });
    };
    window.addEventListener('whisper-chat-insert', handler);
    return () => window.removeEventListener('whisper-chat-insert', handler);
  }, []);

  // Listen for "Add to Chat" events from context menu
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as { files?: File[] } | undefined;
      if (!detail?.files?.length) return;
      // Route through the shared uploader so the chip shows "(uploading…)"
      // and uses the `_uploading_` id prefix the submit guard recognizes.
      void uploadFilesAsChips(detail.files);
    };
    window.addEventListener('whisper-add-to-chat', handler);
    return () => window.removeEventListener('whisper-add-to-chat', handler);
  }, [uploadFilesAsChips]);

  const isStreaming = useActiveChatStore((s) => s.isStreaming);

  const wsConnected = useUIStore((s) => s.wsConnected);

  // Index search picker (point D): which indexed workspaces this session
  // searches. Default = all indexed when no workspace is connected; empty (off)
  // when one is. Sent as selected_search_indexes per turn.
  const { data: indexListData } = useQuery({
    queryKey: ['indexed-workspaces'],
    queryFn: async () => (await listIndexes()).indexes,
    staleTime: 30_000,
  });
  const indexes: IndexInfo[] = useMemo(() => indexListData ?? [], [indexListData]);
  const indexSelection = useIndexSearchStore((s) => s.selectionBySession);
  const setIndexSelection = useIndexSearchStore((s) => s.setSelection);
  const allIndexPaths = useMemo(() => indexes.map((i) => i.path), [indexes]);
  // When a workspace is connected, deprioritize the index: default to an empty
  // selection so the model answers from the workspace and only searches the
  // index on demand (workspace_semantic_search stays available). With no
  // workspace, keep the RAG default of searching every index. `?? default` so
  // an explicit deselect-all is still honored.
  const defaultIndexSel = useMemo(() => (wsConnected ? [] : allIndexPaths), [wsConnected, allIndexPaths]);
  const selectedIndexes = sessionId ? (indexSelection[sessionId] ?? defaultIndexSel) : defaultIndexSel;
  useEffect(() => {
    // Seed the picker to "all" only when NOT connected to a workspace; connected
    // sessions stay empty (index off) until the user opts in.
    if (!wsConnected && sessionId && indexes.length && indexSelection[sessionId] === undefined) {
      setIndexSelection(sessionId, indexes.map((i) => i.path));
    }
  }, [wsConnected, sessionId, indexes, indexSelection, setIndexSelection]);
  const toggleIndex = useCallback((path: string) => {
    if (!sessionId) return;
    const cur = indexSelection[sessionId] ?? indexes.map((i) => i.path);
    setIndexSelection(sessionId, cur.includes(path) ? cur.filter((p) => p !== path) : [...cur, path]);
  }, [sessionId, indexSelection, indexes, setIndexSelection]);

  const models = useSettingsStore((s) => s.models);
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const loadedLocalModel = useSettingsStore((s) => s.loadedLocalModel);
  // On-device model selected? Cloud-only toolbar controls (effort, brief mode)
  // are hidden for local models since they have no effect there.
  const isLocalModel = !!models.find((m) => m.key === selectedModel)?.is_local;

  // Hydrate the slash-command list with the live model keys so /model
  // autocompletes against whatever's defined in config.json.
  const SLASH_COMMANDS: SlashCommand[] = useMemo(
    () =>
      BASE_SLASH_COMMANDS.map((c) =>
        c.cmd === 'model '
          // Show the version label ("Sonnet 4.6") in the picker but insert the
          // model key ("sonnet") the /model handler validates against — so the
          // autocomplete matches the model box under the composer.
          ? { ...c, options: models.map((m) => ({ label: m.name, value: m.key })) }
          : c,
      ),
    [models],
  );
  const setEffortLevel = useSettingsStore((s) => s.setEffortLevel);
  const setVerbosity = useSettingsStore((s) => s.setVerbosity);
  const planMode = useSettingsStore((s) => s.planMode);
  const setPlanMode = useSettingsStore((s) => s.setPlanMode);
  const updateConfig = useSettingsStore((s) => s.updateConfig);
  // Live permission mode for the Mode dropdown's current-state badge.
  // Falls back to 'default' to match the backend's initial value.
  const permissionMode = useSettingsStore((s) => s.config.permissionMode ?? 'default');
  const autoMemory = useSettingsStore((s) => s.autoMemory);
  const setAutoMemory = useSettingsStore((s) => s.setAutoMemory);
  const skills = useSettingsStore((s) => s.skills);
  const mcpServers = useSettingsStore((s) => s.mcpServers);

  const openWorkspaceConnect = useUIStore((s) => s.openWorkspaceConnect);
  const openSettings = useUIStore((s) => s.openSettings);
  const addToast = useUIStore((s) => s.addToast);
  // wsConnected is declared earlier (used by the index-search default).

  // One canonical connect flow: the in-page browser dialog (it has recents,
  // sorting, and a native-picker fallback inside it). Previously this opened
  // the macOS native picker directly, which made the toolbar behave
  // differently from the welcome card — same name kept so the /browse slash
  // command wiring is untouched.
  const handleNativeBrowse = useCallback(() => {
    setWsDropdownOpen(false);
    openWorkspaceConnect();
  }, [openWorkspaceConnect]);

  const handlePlanToggle = useCallback(async () => {
    const newPlan = !planMode;
    setPlanMode(newPlan);
    const targetMode = newPlan ? 'plan' : 'auto';
    updateConfig({ permissionMode: targetMode });
    try {
      await put('/api/permissions/mode', { mode: targetMode });
    } catch (err) {
      console.warn('Failed to set permission mode:', err);
    }
  }, [planMode, setPlanMode, updateConfig]);

  /** Switch the permission mode from the toolbar Mode dropdown. Same write
   *  path as the Settings panel — store, config, then PUT /api/permissions/mode
   *  so the backend session_approvals view stays in sync. The Plan-mode flag
   *  is mirrored so any code that still reads `planMode` keeps working. */
  const handleSelectMode = useCallback(async (target: string) => {
    setModeOpen(false);
    if (target === permissionMode) return;
    setPlanMode(target === 'plan');
    updateConfig({ permissionMode: target });
    try {
      await put('/api/permissions/mode', { mode: target });
    } catch (err) {
      console.warn('Failed to set permission mode:', err);
    }
  }, [permissionMode, setPlanMode, updateConfig]);

  // Autocomplete state machine (slash + @ mention). Destructured into the
  // same local names the component used before the extraction so the
  // submit/keydown/render code below stays unchanged.
  const {
    acItems,
    acVisible,
    acIndex,
    acRect,
    setAcIndex,
    closeAc,
    selectAcItem,
    computeAutocomplete,
  } = useChatAutocomplete({
    text,
    setText,
    textareaRef,
    slashCommands: SLASH_COMMANDS,
    skills,
    mcpServers,
    onAttachWorkspaceFile: attachWorkspaceFileAsChip,
  });

  // Slash-command dispatcher (/effort, /model, /doctor, /help, /btw, …).
  const { handleSlashCommand } = useSlashCommands({
    models,
    setVerbosity,
    setEffortLevel,
    selectedModel,
    autoMemory,
    setAutoMemory,
    openSettings,
    addToast,
    sessionId,
    handleNativeBrowse,
    handlePlanToggle,
    chatStream,
    slashCommands: SLASH_COMMANDS,
    attachWorkspaceFileAsChip,
    uploadWorkspaceFile,
  });

  // Core send logic, parameterized by the raw message text. Both the form
  // (Enter / Send button, which pass the current input) and the voice path
  // (which passes the accumulated dictation directly) funnel through here, so
  // every submit path shares the same guards and skill-prefix handling.
  const submitMessage = useCallback(
    async (raw: string) => {
      const trimmed = raw.trim();
      if (!trimmed) return;

      // Close autocomplete on submit
      closeAc();

      // Check for slash commands first — these work even during streaming
      if (trimmed.startsWith('/')) {
        const handled = handleSlashCommand(trimmed);
        if (handled) {
          inputTextRef.current = '';
          setText('');
          return;
        }
      }

      // "plan approved" (and close variants) → implement the session's latest
      // plan instead of sending the literal text to the model.
      if (/^(plan approved|approve (the )?plan)\.?$/i.test(trimmed)) {
        inputTextRef.current = '';
        setText('');
        void approvePlan();
        return;
      }

      // Regular messages are blocked during streaming
      if (getActiveChatStore().getState().isStreaming) return;

      // Lazy on-device load: in local/hybrid mode a selected model isn't loaded
      // until the session starts. If the chosen model is on-device and not yet
      // resident, load it (behind the banner) before sending so the first turn
      // shows progress instead of a silent multi-second freeze. Abort the send
      // (keeping the typed text) if the load fails. Cloud models skip this.
      {
        const s = useSettingsStore.getState();
        const sel = s.models.find((m) => m.key === s.selectedModel);
        if (sel?.is_local && s.loadedLocalModel !== sel.key) {
          const { loadLocalModel } = await import('@/api/localModel');
          const ready = await loadLocalModel(sel.key, sel.name, s.localContextWindow);
          if (!ready) {
            useUIStore.getState().addToast({
              type: 'error',
              message: `Couldn't load ${sel.name}. Pick a model to start a session.`,
              duration: 5000,
            });
            return;
          }
        }
      }

      // Placeholder chips (id prefixed `_uploading_`) are still mid-upload
      // and have no real backend record. Sending them anyway makes the
      // backend's attachments.get() miss every one, the model receives the
      // bare question with no file context, and replies "I don't see a
      // file". Since uploads now start at attach time (paperclip or /file:),
      // wait for them to settle instead of dropping the send — the
      // conversation starts the moment the upload finishes.
      if (attachments.some(a => a.id.startsWith('_uploading_'))) {
        const cleared = await waitForUploads();
        if (!cleared) {
          useUIStore.getState().addToast({
            type: 'info',
            message: 'Still uploading your file. Try again in a moment.',
            duration: 3000,
          });
          return;
        }
      }

      // Read the freshest attachments (uploads may have resolved during the
      // wait above) and drop any that still failed to settle.
      const settled = attachmentsRef.current.filter(a => !a.id.startsWith('_uploading_'));
      const currentAttachmentIds = settled.map(a => a.id);
      const currentAttachmentNames = settled.map(a => a.filename);
      inputTextRef.current = '';
      setText('');
      setAttachments([]);

      stopMic();
      // Stop the dictation mic if it's running — covers every submit
      // path uniformly: voice command, Enter key, click on the Send button.
      // Slash commands and the early-return guards (empty/streaming/uploading)
      // skip this on purpose so the mic stays on through ephemeral states.

      // Pull an @skills:<name> or @<name> prefix off the front of
      // the message so we can enforce that exact skill server-side
      // (via tool_choice). Without this the prefix is just literal
      // text and the model can — and often does — ignore it. The
      // submenu autocomplete already inserts ``@skills:<name>``;
      // the bare ``@<name>`` form is supported as a convenience for
      // users who type it without the helper.
      const { forceSkill, messageToSend, displayText } = parseSkillMention(trimmed);

      // Delegate to the unified SSE stream handler
      await chatStream.send(messageToSend, {
        attachmentIds: currentAttachmentIds,
        attachmentNames: currentAttachmentNames,
        forceSkill,
        displayText,
      });
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps -- inputTextRef/stopMic are stable (refs/callback from useDictationInput)
    [handleSlashCommand, closeAc, attachments, chatStream, waitForUploads, attachmentsRef, setAttachments, approvePlan],
  );

  const handleSubmit = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault();
      await submitMessage(text);
    },
    [submitMessage, text],
  );

  // Point submitRef at the live submitMessage so the dictation handler (deps [])
  // can submit without depending on it directly.
  useEffect(() => {
    submitRef.current = submitMessage;
    // eslint-disable-next-line react-hooks/exhaustive-deps -- submitRef is a stable ref from useDictationInput
  }, [submitMessage]);

  const handleTextChange = useCallback((e: React.ChangeEvent<HTMLTextAreaElement>) => {
    const val = e.target.value;
    setText(val);
    const cursorPos = e.target.selectionStart;
    computeAutocomplete(val, cursorPos);
  }, [computeAutocomplete]);

  const handleKeyDown = useCallback(
    (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
      // Cmd/Ctrl+Enter always sends, even while the autocomplete popup is up —
      // an explicit "send now" that never gets captured as an item selection.
      if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) {
        e.preventDefault();
        void handleSubmit(e as unknown as React.FormEvent);
        return;
      }

      if (acVisible && acItems.length > 0) {
        if (e.key === 'ArrowDown') {
          e.preventDefault();
          setAcIndex((prev) => (prev + 1) % acItems.length);
          return;
        }
        if (e.key === 'ArrowUp') {
          e.preventDefault();
          setAcIndex((prev) => (prev - 1 + acItems.length) % acItems.length);
          return;
        }
        if (e.key === 'Enter') {
          e.preventDefault();
          selectAcItem(acItems[acIndex]);
          return;
        }
        if (e.key === 'Tab') {
          e.preventDefault();
          selectAcItem(acItems[acIndex]);
          return;
        }
        if (e.key === 'Escape') {
          e.preventDefault();
          closeAc();
          return;
        }
      }

      // Plain Enter sends; Shift+Enter inserts a newline. Enter that confirms
      // an IME composition (CJK input) must not submit.
      if (e.key === 'Enter' && !e.shiftKey && !e.nativeEvent.isComposing) {
        e.preventDefault();
        void handleSubmit(e as unknown as React.FormEvent);
      }
    },
    [acVisible, acItems, acIndex, setAcIndex, selectAcItem, closeAc, handleSubmit],
  );

  /* ── Scroll selected item into view ── */
  useEffect(() => {
    if (!acVisible || !acRef.current) return;
    const selected = acRef.current.children[acIndex] as HTMLElement | undefined;
    selected?.scrollIntoView({ block: 'nearest' });
  }, [acIndex, acVisible]);

  /* ── Auto-grow the composer with its content: expand up to ~2.5x the
        resting two-row height (the CSS max-height clamps the growth), after
        which the textarea scrolls as before. Keyed on `text` so programmatic
        inserts (quote-to-chat, skill mentions) resize too. Looked up by id
        rather than through textareaRef: that ref is owned by
        useChatAutocomplete, and hook arguments must stay unmutated. ── */
  useEffect(() => {
    const ta = document.getElementById('chatInput');
    if (!(ta instanceof HTMLTextAreaElement)) return;
    ta.style.height = 'auto'; // reset so deletions shrink it back
    // +2 for the top/bottom borders: scrollHeight excludes them but the
    // element is border-box, so without it the content gets a scrollbar one
    // line early.
    ta.style.height = `${ta.scrollHeight + 2}px`;
  }, [text]);

  // Abort handler for stop button
  const handleAbort = useCallback(() => {
    chatStream.abort();
  }, [chatStream]);

  return (
    <form className={`chat-input-area${isDragOver ? ' drag-over' : ''}`} id="chatForm" onSubmit={handleSubmit} onDragOver={handleDragOver} onDragLeave={handleDragLeave} onDrop={(e) => void handleDrop(e)}>
      <input
        ref={fileInputRef}
        type="file"
        id="fileInput"
        multiple
        hidden
        accept="image/png,image/jpeg,image/gif,image/webp,audio/*,video/*,.pdf,.docx,.doc,.pptx,.ppt,.xlsx,.xls,.csv,.txt,.md,.json,.xml,.html,.epub,.zip,.py,.js,.ts,.css,.yaml,.yml,.mp3,.wav,.m4a,.flac,.ogg,.aac,.aiff,.mp4,.mov,.webm,.mkv,.avi,.m4v"
        onChange={(e) => void handleFileSelect(e)}
      />

      {/* Attachment preview. Chips grow with the filename but cap their
       * inner label at a sane width — beyond that the name is ellipsized
       * and the full text is revealed on hover via `title`. Removes the
       * old rigid `max-width: 200px` overflow that pushed the × button
       * outside the chip on long names. */}
      <div className="attachment-preview" id="attachmentPreview" style={attachments.length > 0 ? { display: 'flex' } : undefined}>
        {attachments.map((att, idx) => {
          const uploading = att.id.startsWith('_uploading_');
          return (
            <div key={`${att.id}-${idx}`} className="attachment-chip" title={att.filename}>
              <span className="attachment-name">{att.filename}</span>
              {uploading && <span className="attachment-uploading">uploading…</span>}
              <button
                type="button"
                className="attachment-remove"
                aria-label={uploading ? `Cancel upload of ${att.filename}` : `Remove ${att.filename}`}
                onClick={() => removeAttachment(idx)}
              >
                &#xD7;
              </button>
            </div>
          );
        })}
      </div>

      <div className="chat-input-row" style={{ position: 'relative' }}>
        <textarea
          ref={textareaRef}
          className="chat-input"
          id="chatInput"
          aria-label="Chat message input"
          aria-haspopup="listbox"
          aria-expanded={acVisible && acItems.length > 0}
          aria-activedescendant={acVisible && acItems.length > 0 ? `ac-item-${acIndex}` : undefined}
          placeholder="Fix a bug, build a feature, ask anything..."
          rows={2}
          value={text}
          onChange={handleTextChange}
          onKeyDown={handleKeyDown}
        />

        {/* Autocomplete popup — uses vanilla skill-autocomplete CSS classes */}
        {acVisible && acItems.length > 0 && acRect && (
          <div
            ref={acRef}
            className="skill-autocomplete"
            role="listbox"
            aria-label="Autocomplete suggestions"
            style={{
              position: 'fixed',
              left: acRect.left,
              bottom: acRect.bottom,
              width: acRect.width,
            }}
          >
            {acItems.map((item, i) => (
              <div
                key={`${item.name}-${i}`}
                id={`ac-item-${i}`}
                role="option"
                aria-selected={i === acIndex}
                className={`skill-ac-item${i === acIndex ? ' ac-active' : ''}`}
                onMouseDown={(e) => { e.preventDefault(); selectAcItem(item); }}
                onMouseEnter={() => setAcIndex(i)}
              >
                <div className="skill-ac-icon">{item.icon}</div>
                <div className="skill-ac-text">
                  <span className="skill-ac-name">{item.name}</span>
                  <span className="skill-ac-desc">{item.desc}</span>
                </div>
              </div>
            ))}
          </div>
        )}

        <button
          type="button"
          className="attach-btn"
          id="attachBtn"
          title={`Attach files. Supported: ${SUPPORTED_ATTACHMENT_SUMMARY}`}
          onClick={() => fileInputRef.current?.click()}
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21.44 11.05l-9.19 9.19a6 6 0 01-8.49-8.49l9.19-9.19a4 4 0 015.66 5.66l-9.2 9.19a2 2 0 01-2.83-2.83l8.49-8.48"/>
          </svg>
        </button>
        {/* Mic dictation — speak instead of type, transcribed into the input */}
        <button
          type="button"
          className={`mic-btn${mic.isRecording ? ' recording' : ''}${mic.isConnecting ? ' connecting' : ''}`}
          id="chatMicBtn"
          title={
            mic.error
              ? `Mic error: ${mic.error}`
              : mic.isRecording
                ? 'Stop recording'
                : mic.isConnecting
                  ? 'Connecting…'
                  : "Dictate by voice. To send hands-free, finish by saying a phrase like 'okay send' or 'send now'."
          }
          aria-pressed={mic.isRecording}
          aria-label={mic.isRecording ? 'Stop dictation' : 'Start dictation'}
          onClick={handleMicClick}
        >
          {mic.isRecording ? (
            // Stop square — solid fill, rounded, matches the streaming
            // .btn-chat-stop indicator so "stop" reads consistently across
            // the input bar.
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/>
            </svg>
          ) : (
            // Modern microphone — matches the stroke weight and round-cap
            // style of the attach (paperclip) and send (paper-plane) icons
            // beside it. Capsule + stand bracket + base line.
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="9" y="3" width="6" height="11" rx="3"/>
              <path d="M19 11a7 7 0 0 1-14 0"/>
              <line x1="12" y1="18" x2="12" y2="22"/>
            </svg>
          )}
        </button>
        {/* Item 5: Stop button during streaming, send button otherwise */}
        {isStreaming ? (
          <button className="btn btn-chat-stop" type="button" onClick={handleAbort}>
            <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
              <rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor"/>
            </svg>
          </button>
        ) : (
          <button className="btn btn-send" type="submit" id="chatSendBtn" aria-label="Send message" disabled={!text.trim()}>
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <line x1="22" y1="2" x2="11" y2="13"/><polygon points="22 2 15 22 11 13 2 9 22 2"/>
            </svg>
          </button>
        )}
      </div>

      {/* Dictation hint — only while recording. Surfaces the hands-free submit
       *  phrases so users discover them in context, exactly when relevant. */}
      {mic.isRecording && (
        <div className="dictation-hint" role="status" aria-live="polite">
          <span className="dictation-hint-dot" aria-hidden="true" />
          <span className="dictation-hint-label">Listening. Finish your message with one of these to send:</span>
          <span className="dictation-hint-chips">
            {VOICE_SUBMIT_TRIGGERS.map((phrase) => (
              <span className="dictation-hint-chip" key={phrase}>{phrase}</span>
            ))}
          </span>
        </div>
      )}

      {/* Chat toolbar */}
      <div className="chat-toolbar" id="chatToolbar">
        {/* Model dropdown — premium replacement for the native select.
         *  Every selection routes through requestModelChange so Mythos-class
         *  models (Fable 5) always pass the data-retention consent gate. */}
        <ModelDropdown
          models={models}
          selectedModel={selectedModel}
          loadedLocalModel={loadedLocalModel}
          open={modelOpen}
          onToggle={() => {
            setModelOpen((p) => !p);
            setWsDropdownOpen(false);
            setModeOpen(false);
            setMoreOpen(false);
          }}
          onSelect={(key) => { setModelOpen(false); void requestModelChange(key); }}
        />

        <span className="toolbar-sep"></span>

        {/* Effort chip + slider — beside the model, cloud-only (on-device
         *  models have no effort tiers). Single-open: opening closes the rest. */}
        {!isLocalModel && (
          <>
            <EffortPicker
              onOpen={() => {
                setModelOpen(false); setWsDropdownOpen(false);
                setModeOpen(false); setMoreOpen(false);
              }}
            />
            <ResponseLengthPicker
              onOpen={() => {
                setModelOpen(false); setWsDropdownOpen(false);
                setModeOpen(false); setMoreOpen(false);
              }}
            />
            <span className="toolbar-sep"></span>
          </>
        )}

        {/* Workspace dropdown */}
        <WorkspaceDropdown
          connected={wsConnected}
          open={wsDropdownOpen}
          recentPaths={recentPaths}
          onToggle={() => { setWsDropdownOpen(!wsDropdownOpen); setModeOpen(false); setModelOpen(false); setMoreOpen(false); }}
          onBrowse={() => handleNativeBrowse()}
          onConnect={(p) => void connectToWorkspace(p)}
        />

        <span className="toolbar-sep"></span>

        {/* Mode dropdown — switch permission mode without leaving the chat.
         *  Same write path as Settings → Permissions. */}
        <ModeDropdown
          permissionMode={permissionMode}
          open={modeOpen}
          onToggle={() => {
            setModeOpen((p) => !p);
            setWsDropdownOpen(false); setModelOpen(false); setMoreOpen(false);
          }}
          onSelect={handleSelectMode}
          onManage={() => { setModeOpen(false); openSettings('permissions'); }}
        />

        {/* Local-model controls — render only for on-device models. */}
        <LocalToggles
          onOpen={() => {
            setModelOpen(false); setWsDropdownOpen(false);
            setModeOpen(false); setMoreOpen(false);
          }}
        />
        <LocalContextWindowSlider
          onOpen={() => {
            setModelOpen(false); setWsDropdownOpen(false);
            setModeOpen(false); setMoreOpen(false);
          }}
        />

        {/* + More — overflow for the less-frequent toolbar controls. */}
        <MoreMenu
          open={moreOpen}
          section={moreSection}
          setSection={setMoreSection}
          onToggle={() => {
            setMoreOpen((p) => {
              const next = !p;
              if (next) { setModelOpen(false); setWsDropdownOpen(false); setModeOpen(false); }
              return next;
            });
          }}
          onClose={() => setMoreOpen(false)}
          indexes={indexes}
          selectedIndexes={selectedIndexes}
          toggleIndex={toggleIndex}
          wsConnected={wsConnected}
          onInsertSkill={(name) => {
            setText((prev) => `@${name} ${prev}`);
            setMoreOpen(false);
            textareaRef.current?.focus();
          }}
        />

        {/* Token counter — reads from chatStore instead of DOM */}
        <TokenCounter />
      </div>
    </form>
  );
};
