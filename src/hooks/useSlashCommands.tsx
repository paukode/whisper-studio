import React, { useCallback } from 'react';
import { get } from '@/api/client';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSubagentStore } from '@/stores/subagentStore';
import { applyTeamProgressToMessage } from '@/hooks/chatStream/teamProgress';
import type { ChatMessage, TeamAgentReport, TeamProgressEvent } from '@/types/chat';
import { useUIStore, type UIState } from '@/stores/uiStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useGoalStore } from '@/stores/goalStore';
import { launchRun } from '@/api/workflows';
import { startWatch, planAutofix } from '@/api/ci';
import { useSettingsStore } from '@/stores/settingsStore';
import type { SettingsState } from '@/stores/settingsStore';
import type { UseChatStreamReturn } from '@/hooks/useChatStream';
import { requestModelChange } from '@/components/chat/dataRetentionConsent';
import { clampEffort, effortLabel, EFFORT_ORDER } from '@/utils/effort';
import { useTheme } from '@/providers/ThemeProvider';
import type { ThemeKey } from '@/types/theme';
import {
  HELP_CATEGORY_LABELS,
  HELP_CATEGORY_ORDER,
  type ModelOption,
  type SlashCommand,
} from '@/components/chat/chatInputConstants';

export interface UseSlashCommandsOptions {
  models: ModelOption[];
  setVerbosity: SettingsState['setVerbosity'];
  setEffortLevel: SettingsState['setEffortLevel'];
  selectedModel: string;
  autoMemory: boolean;
  setAutoMemory: SettingsState['setAutoMemory'];
  openSettings: UIState['openSettings'];
  addToast: UIState['addToast'];
  sessionId: string | null;
  handleNativeBrowse: () => Promise<void> | void;
  handlePlanToggle: () => Promise<void> | void;
  chatStream: UseChatStreamReturn;
  /** Already-hydrated slash command list. */
  slashCommands: SlashCommand[];
  /** Attach a workspace file to the composer as a chip (background upload, no
   *  send) — used by `/file:path` with no trailing question. */
  attachWorkspaceFileAsChip: (path: string) => Promise<void> | void;
  /** Fetch + upload a workspace file and return its attachment record (no
   *  chip) — used by the non-blocking `/file:path question` auto-send. */
  uploadWorkspaceFile: (path: string) => Promise<{ id: string; filename: string } | null>;
}

export interface UseSlashCommandsResult {
  /** Returns true if `input` was a recognised slash command (and handled
   *  it), false if the caller should treat the text as a normal message. */
  handleSlashCommand: (input: string) => boolean;
}

/**
 * Slash-command dispatcher for the chat composer. Extracted verbatim from
 * ChatInput.tsx — interprets `/effort`, `/model`, `/doctor`, `/help`,
 * `/btw`, `/rename`, `/subagent`, `/file:…`, and the rest. Side effects go
 * through the same store actions and SSE stream the component used; this
 * hook just owns the (large) switch so the component body stays readable.
 *
 * Dependencies are passed in and aliased to their original local names
 * (SLASH_COMMANDS) so the moved switch body is byte-for-byte identical.
 */
export function useSlashCommands(opts: UseSlashCommandsOptions): UseSlashCommandsResult {
  const {
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
  } = opts;

  // In-tab theme setter — the same one the Settings/Header theme picker uses.
  const { setTheme } = useTheme();

  /* ── Slash command handler (on submit) ── */
  const handleSlashCommand = useCallback((input: string): boolean => {
    if (!input.startsWith('/')) return false;

    const parts = input.slice(1).split(/\s+/);
    const cmd = parts[0]?.toLowerCase();
    const arg = parts[1]?.toLowerCase();

    // /file:path [question] — attach a workspace file. With NO trailing
    // question it just attaches a composer chip (upload in the background) and
    // the user keeps composing. With a question it sends right away, but
    // non-blocking: the message + working indicator appear immediately while
    // the file is fetched/converted/uploaded in the background.
    if (cmd?.startsWith('file:')) {
      const filePath = cmd.slice(5);
      const question = parts.slice(1).join(' ').trim();
      if (!filePath) {
        addToast({ type: 'info', message: 'Usage: /file:path [question]', duration: 3000 });
        return true;
      }
      const fileName = filePath.split('/').pop() || filePath;

      if (!question) {
        // Attach only — chip + background upload, no send.
        void attachWorkspaceFileAsChip(filePath);
        return true;
      }

      // Show the user message + thinking indicator immediately so the
      // conversation visibly starts; the model call fires once the file is
      // uploaded (PDF→text conversion can take a moment).
      const ts = new Date().toISOString();
      const chat = () => getChatStore(sessionId).getState();
      chat().addMessage({ role: 'user', content: question, timestamp: ts, attachmentNames: [fileName] });
      chat().setStreaming(true);

      const patchMessage = (patch: Partial<ChatMessage>) => {
        const st = chat();
        const idx = st.messages.findIndex((m) => m.timestamp === ts);
        if (idx < 0) return;
        const updated = [...st.messages];
        updated[idx] = { ...updated[idx], ...patch };
        st.setMessages(updated);
      };

      void (async () => {
        const att = await uploadWorkspaceFile(filePath);
        if (!att) {
          chat().setStreaming(false);
          patchMessage({ content: `${question}\n\n_[Could not attach ${fileName}]_` });
          addToast({ type: 'error', message: `Could not attach ${fileName}`, duration: 3000 });
          return;
        }
        // Record the real id on the already-shown message so a later
        // regenerate re-attaches the same file (mirrors the /subagent patch).
        patchMessage({ attachmentIds: [att.id], attachmentNames: [att.filename] });
        await chatStream.send(question, {
          attachmentIds: [att.id],
          attachmentNames: [att.filename],
          hideUserMessage: true,
        });
      })();
      return true;
    }

    switch (cmd) {
      case 'effort': {
        const allowed = models.find((m) => m.key === selectedModel)?.effort_levels ?? [];
        if (allowed.length === 0) {
          addToast({ type: 'info', message: 'This model has no effort level', duration: 2500 });
          return true;
        }
        // Accept the raw API name `xhigh` as an alias for the Extra label.
        const key = arg === 'xhigh' ? 'extra' : arg;
        if (key && allowed.includes(key)) {
          setEffortLevel(key);
          addToast({ type: 'success', message: `Effort set to ${effortLabel(key)}`, duration: 2000 });
        } else if (key && EFFORT_ORDER.includes(key as never)) {
          // Valid level, just not on this model — clamp to nearest lower.
          const clamped = clampEffort(key, allowed);
          setEffortLevel(clamped);
          addToast({ type: 'info', message: `${effortLabel(key)} isn't available here; set to ${effortLabel(clamped)}`, duration: 3000 });
        } else {
          addToast({ type: 'info', message: `Usage: /effort ${allowed.map(effortLabel).join('|')}`, duration: 3000 });
        }
        return true;
      }
      case 'model': {
        const validKeys = models.map((m) => m.key);
        if (arg && validKeys.includes(arg)) {
          // Route through the data-retention gate — Mythos-class models
          // (Fable 5) must show the consent screen on EVERY switch, no
          // matter which surface initiates it. Only toast if the switch
          // actually happened (the user may cancel the consent dialog).
          // Confirm with the version label ("Sonnet 4.6"), matching the model
          // box and the autocomplete — not the raw key ("sonnet").
          const pickedLabel = models.find((m) => m.key === arg)?.name ?? arg;
          void requestModelChange(arg).then((switched) => {
            if (switched) {
              addToast({ type: 'success', message: `Model set to ${pickedLabel}`, duration: 2000 });
            }
          });
        } else {
          const usage = validKeys.length > 0 ? validKeys.join('|') : 'haiku|sonnet|opus4.8';
          addToast({ type: 'info', message: `Usage: /model ${usage}`, duration: 3000 });
        }
        return true;
      }
      case 'verbosity': {
        // Verbosity is the unified Response length control (stored as GPT-5.x's
        // native verbosity low/medium/high; shown as Brief/Normal/Detailed).
        // Accept either the visible labels or the raw stored levels.
        const alias: Record<string, 'low' | 'medium' | 'high'> = {
          brief: 'low', low: 'low',
          normal: 'medium', medium: 'medium',
          detailed: 'high', high: 'high',
        };
        const next = arg ? alias[arg] : undefined;
        if (next) {
          setVerbosity(next);
          const label = next === 'low' ? 'Brief' : next === 'medium' ? 'Normal' : 'Detailed';
          addToast({ type: 'success', message: `Response length: ${label}`, duration: 2000 });
        } else {
          addToast({ type: 'info', message: 'Usage: /verbosity brief|normal|detailed', duration: 3000 });
        }
        return true;
      }
      case 'theme': {
        const validThemes: ThemeKey[] = ['auto', 'dark', 'light', 'dark-high-contrast', 'light-high-contrast', 'dark-daltonized', 'light-daltonized', 'dark-taw', 'light-taw'];
        if (arg && (validThemes as string[]).includes(arg)) {
          // Drive the in-tab theme setter (same one the Settings/Header picker
          // uses). It updates ThemeProvider's React state — so it re-renders and
          // every theme consumer (Monaco, diff viewer, the picker) updates
          // immediately — and persists to localStorage. The old approach
          // dispatched a StorageEvent, which never fires in the tab that wrote
          // it, so /theme silently did nothing until a reload.
          setTheme(arg as ThemeKey);
          addToast({ type: 'success', message: `Theme set to ${arg}`, duration: 2000 });
        } else {
          addToast({ type: 'info', message: `Usage: /theme ${validThemes.join('|')}`, duration: 4000 });
        }
        return true;
      }
      case 'doctor': {
        void (async () => {
          try {
            const result = await get<Record<string, unknown>>('/api/doctor');
            const status = String(result.status ?? 'ok');
            const checks = Array.isArray(result.checks)
              ? (result.checks as Array<{ check: string; status: string; detail?: string }>)
              : [];

            const sIcon = status === 'ok' ? '✓' : status === 'warn' ? '⚠' : '✗';
            const sColor = status === 'ok' ? '#5dba6e' : status === 'warn' ? '#ffa657' : '#e06060';

            const dotColor = (s: string) => s === 'ok' ? '#5dba6e' : s === 'warn' ? '#ffa657' : '#e06060';
            const rowStyle: React.CSSProperties = { display: 'flex', alignItems: 'flex-start', gap: 10, padding: '7px 12px', borderBottom: '1px solid var(--border)' };
            const dotStyle = (c: string): React.CSSProperties => ({ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, marginTop: 3, background: c });

            let rows: React.ReactNode[];
            if (checks.length > 0) {
              rows = checks.map((c, i) => (
                <div key={i} style={rowStyle}>
                  <span style={dotStyle(dotColor(c.status))} />
                  <span style={{ fontWeight: 500, minWidth: 120, flexShrink: 0 }}>{c.check}</span>
                  <span style={{ color: 'var(--text-muted)', wordBreak: 'break-all' }}>{c.detail ?? ''}</span>
                </div>
              ));
            } else {
              rows = Object.entries(result)
                .filter(([k]) => k !== 'status' && k !== 'checks')
                .map(([k, v]) => {
                  const detail = typeof v === 'object' && v !== null ? JSON.stringify(v) : String(v);
                  return (
                    <div key={k} style={rowStyle}>
                      <span style={dotStyle('#5dba6e')} />
                      <span style={{ fontWeight: 500, minWidth: 120, flexShrink: 0 }}>{k}</span>
                      <span style={{ color: 'var(--text-muted)', wordBreak: 'break-all' }}>{detail}</span>
                    </div>
                  );
                });
            }

            const body = (
              <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden', fontSize: 12 }}>
                <div style={{ padding: '8px 12px', background: 'var(--bg-elevated)', borderBottom: '1px solid var(--border)', fontWeight: 600, fontSize: 12, display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ color: sColor }}>{sIcon}</span> Diagnostics: {status}
                </div>
                {rows}
              </div>
            );

            useUIStore.getState().pushDialog({ kind: 'open', title: false, size: 'md', body });
          } catch {
            addToast({ type: 'error', message: 'Doctor check failed.', duration: 3000 });
          }
        })();
        return true;
      }
      case 'export': {
        const messages = getChatStore(sessionId).getState().messages;
        const exported = messages.map((m) => `[${m.role}] ${m.content}`).join('\n\n---\n\n');
        const blob = new Blob([exported], { type: 'text/plain' });
        const url = URL.createObjectURL(blob);
        const a = document.createElement('a');
        a.href = url;
        a.download = `chat-export-${Date.now()}.txt`;
        a.click();
        URL.revokeObjectURL(url);
        addToast({ type: 'success', message: 'Chat exported.', duration: 2000 });
        return true;
      }
      case 'skills': {
        openSettings('skills');
        return true;
      }
      case 'btw': {
        if (!arg) {
          addToast({ type: 'info', message: 'Usage: /btw <question>', duration: 3000 });
          return true;
        }
        const fullQuestion = input.slice(5).trim();
        addToast({ type: 'info', message: `Asking: ${fullQuestion}…`, duration: 3000 });
        void (async () => {
          try {
            const response = await fetch('/api/chat/btw', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({
                question: fullQuestion,
                model: selectedModel,
                recent_history: getChatStore(sessionId).getState().messages.slice(-4).map(m => ({ role: m.role, content: m.content })),
              }),
            });
            if (!response.ok || !response.body) throw new Error('BTW request failed');
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let btwContent = '';
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() ?? '';
              for (const line of lines) {
                const trimmedLine = line.trim();
                if (!trimmedLine.startsWith('data: ')) continue;
                const payload = trimmedLine.slice(6);
                if (payload === '[DONE]') continue;
                try {
                  const evt: Record<string, unknown> = JSON.parse(payload);
                  if (typeof evt.text === 'string') btwContent += evt.text;
                } catch { /* skip malformed JSON */ }
              }
            }
            if (btwContent) {
              useUIStore.getState().setBtwPopup({ question: fullQuestion, answer: btwContent });
            }
          } catch {
            addToast({ type: 'error', message: 'BTW request failed.', duration: 3000 });
          }
        })();
        return true;
      }
      case 'rename': {
        const newTitle = input.slice(8).trim(); // everything after "/rename "
        if (newTitle && sessionId) {
          useSessionStore.getState().updateSessionTitle(sessionId, newTitle, true);
          fetch(`/api/sessions/${sessionId}/title`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ title: newTitle, customTitle: true }),
          }).catch(() => {});
          addToast({ type: 'success', message: `Session renamed to "${newTitle}"`, duration: 2000 });
        } else {
          addToast({ type: 'info', message: 'Usage: /rename <new title>', duration: 3000 });
        }
        return true;
      }
      case 'workflow': {
        if (!sessionId) {
          addToast({ type: 'info', message: 'Open a session first.', duration: 2000 });
          return true;
        }
        const wfName = input.slice(10).trim().split(/\s+/)[0]; // after "/workflow "
        if (!wfName) {
          addToast({ type: 'info', message: 'Usage: /workflow <saved-name>', duration: 3000 });
          return true;
        }
        void (async () => {
          try {
            const r = await launchRun({ name: wfName, session_id: sessionId });
            getChatStore(sessionId).getState().addMessage({
              role: 'assistant',
              content: '',
              timestamp: new Date().toISOString(),
              toolUse: [{ toolId: 'workflow_started', toolName: 'workflow_started', input: { run_id: r.run_id, name: wfName }, status: 'complete' }],
            });
          } catch {
            addToast({ type: 'error', message: `Could not run workflow '${wfName}'. Is it saved?`, duration: 3000 });
          }
        })();
        return true;
      }
      case 'ci': {
        if (!sessionId) {
          addToast({ type: 'info', message: 'Open a session first.', duration: 2000 });
          return true;
        }
        const parts = input.slice(3).trim().split(/\s+/).filter(Boolean); // after "/ci"
        const sub = parts[0] || '';
        void (async () => {
          try {
            if (sub === 'autofix') {
              const s = useSettingsStore.getState();
              const modelId = s.config.chatModels[s.selectedModel];
              const plan = await planAutofix({ branch: parts[1], session_id: sessionId });
              const chat = getChatStore(sessionId).getState();
              if (plan.findings?.length) {
                chat.addMessage({
                  role: 'assistant', content: '', timestamp: new Date().toISOString(),
                  toolUse: [{ toolId: 'ci_diagnosis', toolName: 'ci_diagnosis', input: { branch: plan.branch, run_id: plan.run_id, url: plan.url, findings: plan.findings }, status: 'complete' }],
                });
              }
              if (plan.script) {
                chat.addMessage({
                  role: 'assistant', content: '', timestamp: new Date().toISOString(),
                  toolUse: [{ toolId: 'workflow_preview', toolName: 'workflow_preview', input: { script: plan.script, name: 'ci-autofix', description: plan.summary, phases: [{ title: 'Fix' }, { title: 'Verify' }], budget_usd: plan.budget_usd ?? null, model_id: modelId }, status: 'complete' }],
                });
              } else {
                addToast({ type: 'info', message: plan.summary, duration: 4000 });
              }
            } else {
              const r = await startWatch({ branch: sub || undefined, session_id: sessionId });
              getChatStore(sessionId).getState().addMessage({
                role: 'assistant', content: '', timestamp: new Date().toISOString(),
                toolUse: [{ toolId: 'ci_started', toolName: 'ci_started', input: { task_id: r.task_id, branch: r.branch }, status: 'complete' }],
              });
            }
          } catch {
            addToast({ type: 'error', message: 'CI unavailable — is the GitHub CLI (gh) installed and authed?', duration: 3500 });
          }
        })();
        return true;
      }
      case 'goal': {
        if (!sessionId) {
          addToast({ type: 'info', message: 'Open a session first.', duration: 2000 });
          return true;
        }
        const goalText = input.slice(6).trim(); // everything after "/goal "
        if (goalText.toLowerCase() === 'clear') {
          useGoalStore.getState().clearGoal(sessionId);
          void fetch(`/api/sessions/${sessionId}/goal`, { method: 'DELETE' }).catch(() => {});
          addToast({ type: 'success', message: 'Goal cleared.', duration: 2000 });
        } else if (!goalText) {
          addToast({ type: 'info', message: 'Usage: /goal <text>  ·  /goal clear', duration: 3000 });
        } else {
          useGoalStore.getState().setGoal(sessionId, goalText, true);
          void fetch(`/api/sessions/${sessionId}/goal`, {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ goal: goalText }),
          }).catch(() => {});
          addToast({ type: 'success', message: 'Goal set — the loop will work toward it.', duration: 2500 });
        }
        return true;
      }
      case 'memory': {
        if (arg === 'on') {
          setAutoMemory(true);
          addToast({ type: 'success', message: 'Memory enabled', duration: 2000 });
        } else if (arg === 'off') {
          setAutoMemory(false);
          addToast({ type: 'success', message: 'Memory disabled', duration: 2000 });
        } else if (arg === 'status') {
          addToast({ type: 'info', message: `Memory is ${autoMemory ? 'enabled' : 'disabled'}`, duration: 2000 });
        } else {
          addToast({ type: 'info', message: 'Usage: /memory on|off|status', duration: 3000 });
        }
        return true;
      }
      case 'help': {
        const helpSections: React.ReactNode[] = [];
        for (const cat of HELP_CATEGORY_ORDER) {
          const cmds = SLASH_COMMANDS.filter(c => c.category === cat);
          if (cmds.length === 0) continue;
          helpSections.push(
            <div key={`cat-${cat}`} style={{ fontSize: '0.75em', fontWeight: 600, textTransform: 'uppercase', letterSpacing: '0.05em', color: 'var(--text-muted)', margin: '14px 0 6px', paddingBottom: 4, borderBottom: '1px solid var(--border)' }}>
              {HELP_CATEGORY_LABELS[cat] ?? cat}
            </div>,
          );
          for (const c of cmds) {
            helpSections.push(
              <div key={`cmd-${c.cmd}`} style={{ display: 'flex', alignItems: 'center', gap: 10, padding: '5px 0', fontSize: '0.88em' }}>
                <span style={{ flexShrink: 0, width: 24, textAlign: 'center' }}>{c.icon}</span>
                <span style={{ fontWeight: 500, color: 'var(--accent)', minWidth: 80 }}>/{c.cmd.trim()}</span>
                <span style={{ color: 'var(--text-secondary)' }}>{c.desc}</span>
              </div>,
            );
          }
        }
        useUIStore.getState().pushDialog({
          kind: 'open',
          title: 'Available Commands',
          size: 'md',
          body: <div style={{ maxHeight: '60vh', overflow: 'auto' }}>{helpSections}</div>,
        });
        return true;
      }
      case 'clear': {
        getChatStore(sessionId).getState().clearMessages();
        addToast({ type: 'info', message: 'Chat cleared', duration: 2000 });
        return true;
      }
      case 'settings': {
        openSettings('apikeys');
        return true;
      }
      case 'workspace': {
        void handleNativeBrowse();
        return true;
      }
      case 'notify': {
        if (!('Notification' in window)) {
          addToast({ type: 'warning', message: 'Notifications not supported in this browser', duration: 3000 });
        } else if (Notification.permission === 'granted') {
          addToast({ type: 'info', message: 'Notifications are already enabled', duration: 2000 });
        } else if (Notification.permission === 'denied') {
          addToast({ type: 'warning', message: 'Notifications blocked. Reset site permissions in browser settings.', duration: 4000 });
        } else {
          void Notification.requestPermission().then(perm => {
            if (perm === 'granted') addToast({ type: 'success', message: 'Notifications enabled', duration: 2000 });
            else addToast({ type: 'warning', message: 'Notifications blocked', duration: 3000 });
          });
        }
        return true;
      }
      case 'plan': {
        void handlePlanToggle();
        return true;
      }
      case 'subagent': {
        // Run a background agent with the full tool loop (web fetch/search,
        // workspace, code, and all enabled MCP tools incl. the AgentCore
        // browser). It streams live progress into a TeamReportCard and does
        // NOT block the composer — the user can keep chatting while it works.
        const task = input.slice('/subagent '.length).trim();
        if (!task) {
          addToast({ type: 'info', message: 'Usage: /subagent <task>', duration: 3000 });
          return true;
        }
        const chat = () => getChatStore(sessionId).getState();
        // The request as a normal user message + a dedicated assistant message
        // that hosts the live progress card and (later) the final answer. The
        // assistant message opens with an intro so the user knows work is
        // happening in the background and that they can keep chatting.
        const INTRO = '🤖 **Subagent started**: working on this in the background. '
          + 'Keep chatting; its live progress is in the card below, and it will post '
          + 'the result here when done. Use **Stop** on the card to cancel it.';
        // Distinct timestamps: two synchronous toISOString() calls land on the
        // same millisecond, and timestamps double as message ids — a collision
        // would make the progress locator match the user message and drop the
        // card's data.
        const baseTs = Date.now();
        chat().addMessage({ role: 'user', content: task, timestamp: new Date(baseTs).toISOString() });
        const subTs = new Date(baseTs + 1).toISOString();
        chat().addMessage({ role: 'assistant', content: INTRO, timestamp: subTs });

        // Stable id so the backend stream and the card's Stop button refer to
        // the same run.
        const teamId = `subagent-${crypto.randomUUID()}`;
        const controller = new AbortController();
        let stopped = false;
        useSubagentStore.getState().register(teamId, () => {
          stopped = true;
          controller.abort();
        });

        const patch = (fn: (m: ChatMessage) => ChatMessage) => {
          const st = chat();
          const idx = st.messages.findIndex((m) => m.timestamp === subTs && m.role === 'assistant');
          if (idx < 0) return;
          const next = [...st.messages];
          next[idx] = fn(next[idx]);
          st.setMessages(next);
        };

        void (async () => {
          try {
            const response = await fetch('/api/subagent/stream', {
              method: 'POST',
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify({ task, model: selectedModel, session_id: sessionId ?? '', team_id: teamId }),
              signal: controller.signal,
            });
            if (!response.ok || !response.body) throw new Error(`subagent HTTP ${response.status}`);
            const reader = response.body.getReader();
            const decoder = new TextDecoder();
            let buffer = '';
            let finalOutput = '';
            for (;;) {
              const { done, value } = await reader.read();
              if (done) break;
              buffer += decoder.decode(value, { stream: true });
              const lines = buffer.split('\n');
              buffer = lines.pop() ?? '';
              for (const line of lines) {
                const trimmedLine = line.trim();
                if (!trimmedLine.startsWith('data: ')) continue;
                const payload = trimmedLine.slice(6);
                if (payload === '[DONE]') continue;
                let evt: Record<string, unknown>;
                try { evt = JSON.parse(payload); } catch { continue; }
                if (evt.team_progress) {
                  applyTeamProgressToMessage(chat, subTs, evt.team_progress as TeamProgressEvent);
                } else if (evt.subagent_done) {
                  const d = evt.subagent_done as { output?: string };
                  finalOutput = (d.output ?? '').trim();
                } else if (typeof evt.error === 'string') {
                  finalOutput = `[Subagent error] ${evt.error}`;
                }
              }
            }
            patch((m) => ({ ...m, content: finalOutput || '[subagent returned no output]' }));
          } catch (err) {
            if (stopped) {
              // User pressed Stop — mark the team done and flip any still-running
              // agent rows to "stopped" so they don't spin forever, and note it.
              patch((m) => {
                const tr = m.teamReports?.[teamId];
                if (!tr) return { ...m, content: '⏹ Subagent stopped.' };
                const agents: Record<string, TeamAgentReport> = {};
                for (const [k, a] of Object.entries(tr.agents)) {
                  agents[k] = (a.status === 'running' || a.status === 'pending')
                    ? { ...a, status: 'stopped' }
                    : a;
                }
                return {
                  ...m,
                  content: '⏹ Subagent stopped.',
                  teamReports: { ...m.teamReports, [teamId]: { ...tr, status: 'completed', agents } },
                };
              });
            } else {
              const msg = err instanceof Error ? err.message : String(err);
              patch((m) => ({ ...m, content: `[Subagent error] ${msg}` }));
              addToast({ type: 'error', message: `Subagent failed: ${msg}`, duration: 4000 });
            }
          } finally {
            useSubagentStore.getState().unregister(teamId);
          }
        })();
        return true;
      }
      default:
        return false;
    }
  }, [setVerbosity, setEffortLevel, setTheme, openSettings, addToast, selectedModel, autoMemory, setAutoMemory, sessionId, handleNativeBrowse, handlePlanToggle, chatStream, SLASH_COMMANDS, models, attachWorkspaceFileAsChip, uploadWorkspaceFile]);

  return { handleSlashCommand };
}
