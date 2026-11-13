import React, { useCallback } from 'react';
import { get } from '@/api/client';
import { getChatStore } from '@/stores/sessionRuntimes';
import { runCompact } from '@/hooks/compactCommand';
import { clearConversation } from '@/hooks/chatStream/clearConversation';
import { useUIStore, type UIState } from '@/stores/uiStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useGoalStore } from '@/stores/goalStore';
import { launchRun } from '@/api/workflows';
import { startWatch, planAutofix } from '@/api/ci';
import { clearSessionGoal, setSessionGoal } from '@/api/goals';
import { renameSession } from '@/api/sessions';
import { toError } from '@/utils/toError';
import { activeEffortLevel, noChatModelHint, useSettingsStore } from '@/stores/settingsStore';
import type { SettingsState } from '@/stores/settingsStore';
import type { UseChatStreamReturn } from '@/hooks/useChatStream';
import { requestModelChange } from '@/components/chat/dataRetentionConsent';
import { ContextReportPanel, type ContextReport } from '@/components/chat/ContextReportPanel';
import { clampEffort, effortLabel, EFFORT_ORDER, thinkingLabel } from '@/utils/effort';
import { downloadFile } from '@/utils/downloadFile';
import { ICON } from '@/components/chat/activityIcons';
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
}

export interface UseSlashCommandsResult {
  /** Returns true if `input` was a recognised slash command (and handled
   *  it), false if the caller should treat the text as a normal message. */
  handleSlashCommand: (input: string) => boolean;
}

/**
 * Slash-command dispatcher for the chat composer: interprets the built-in
 * commands (`/effort`, `/model`, `/doctor`, `/help`, `/rename` and the
 * rest). Skills and MCP prompts are no commands here: ChatInput sends them
 * (src/components/chat/composerMenus.ts). Side effects go through the store actions and the chat
 * stream; this hook owns the (large) switch so ChatInput stays readable.
 */
export function useSlashCommands(opts: UseSlashCommandsOptions): UseSlashCommandsResult {
  const {
    models,
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
  } = opts;

  // In-tab theme setter — the same one the Settings/Header theme picker uses.
  const { setTheme } = useTheme();

  /* ── Slash command handler (on submit) ── */
  const handleSlashCommand = useCallback((input: string): boolean => {
    if (!input.startsWith('/')) return false;

    const parts = input.slice(1).split(/\s+/);
    const cmd = parts[0]?.toLowerCase();
    const arg = parts[1]?.toLowerCase();

    switch (cmd) {
      case 'effort': {
        const picked = models.find((m) => m.key === selectedModel);
        const allowed = picked?.effort_levels ?? [];
        // On an on-device model /effort sets its Thinking level (Off to Max),
        // never the cloud effort.
        const local = !!picked?.is_local;
        const kind = local ? 'Thinking' : 'Effort';
        const label = local ? thinkingLabel : effortLabel;
        const setLevel = local ? useSettingsStore.getState().setLocalThinkingLevel : setEffortLevel;
        if (allowed.length === 0) {
          addToast({ type: 'info', message: `This model has no ${kind.toLowerCase()} level`, duration: 2500, persist: false });
          return true;
        }
        // Accept the raw API name `xhigh` as an alias for the Extra label, and
        // `off` for no thinking.
        const key = arg === 'xhigh' ? 'extra' : arg === 'off' ? 'none' : arg;
        if (key && allowed.includes(key)) {
          setLevel(key);
          addToast({ type: 'success', message: `${kind} set to ${label(key)}`, duration: 2000, persist: false });
        } else if (key && EFFORT_ORDER.includes(key as never)) {
          // Valid level, just not on this model — clamp to nearest lower.
          const clamped = clampEffort(key, allowed);
          setLevel(clamped);
          addToast({ type: 'info', message: `${label(key)} isn't available here; set to ${label(clamped)}`, duration: 3000, persist: false });
        } else {
          addToast({ type: 'info', message: `Usage: /effort ${allowed.map(label).join('|')}`, duration: 3000, persist: false });
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
          // Confirm with the version label ("Sonnet 5.5"), matching the model
          // box and the autocomplete, not the raw key ("sonnet5.5").
          const pickedLabel = models.find((m) => m.key === arg)?.name ?? arg;
          void requestModelChange(arg).then((switched) => {
            if (switched) {
              addToast({ type: 'success', message: `Model set to ${pickedLabel}`, duration: 2000, persist: false });
            }
          });
        } else if (validKeys.length === 0) {
          // Nothing to pick (Local mode before an on-device model is installed):
          // say where to get one, never list cloud keys the mode cannot run.
          const hint = noChatModelHint(useSettingsStore.getState().needsLocalModel);
          addToast({ type: 'info', message: hint, duration: 6000, persist: false });
        } else {
          addToast({ type: 'info', message: `Usage: /model ${validKeys.join('|')}`, duration: 3000, persist: false });
        }
        return true;
      }
      case 'theme': {
        const validThemes: ThemeKey[] = ['auto', 'light', 'dark'];
        if (arg && (validThemes as string[]).includes(arg)) {
          // Drive the in-tab theme setter (same one the Settings/Header picker
          // uses). It updates ThemeProvider's React state — so it re-renders and
          // every theme consumer (Monaco, diff viewer, the picker) updates
          // immediately — and persists to localStorage. The old approach
          // dispatched a StorageEvent, which never fires in the tab that wrote
          // it, so /theme silently did nothing until a reload.
          setTheme(arg as ThemeKey);
          addToast({ type: 'success', message: `Theme set to ${arg}`, duration: 2000, persist: false });
        } else {
          addToast({ type: 'info', message: `Usage: /theme ${validThemes.join('|')}`, duration: 4000, persist: false });
        }
        return true;
      }
      case 'doctor': {
        // `/doctor context` reports what the model is actually being sent: which
        // prompt sections cost what, how much of that is cached, and whether any
        // of it duplicates a tool description or names a tool that is gone.
        if (arg === 'context') {
          void (async () => {
            try {
              const r = await get<ContextReport>('/api/doctor/context');
              useUIStore.getState().pushDialog({
                kind: 'open', title: false, size: 'lg', body: <ContextReportPanel report={r} />,
              });
            } catch (e) {
              addToast({
                type: 'error',
                message: e instanceof Error ? e.message : 'Context report failed.',
                duration: 3000,
              });
            }
          })();
          return true;
        }
        void (async () => {
          try {
            // server/doctor.py always answers {status, checks}.
            const result = await get<{
              status: 'ok' | 'warn' | 'error';
              checks: Array<{ check: string; status: string; detail?: string }>;
            }>('/api/doctor');
            const { status, checks } = result;

            const sIcon = status === 'ok' ? ICON.check : status === 'warn' ? ICON.alert : ICON.x;
            const sColor = status === 'ok' ? 'var(--success)' : status === 'warn' ? 'var(--warning)' : 'var(--danger)';

            const dotColor = (s: string) => s === 'ok' ? 'var(--success)' : s === 'warn' ? 'var(--warning)' : 'var(--danger)';
            const rowStyle: React.CSSProperties = { display: 'flex', alignItems: 'flex-start', gap: 10, padding: '7px 12px', borderBottom: '1px solid var(--border)' };
            const dotStyle = (c: string): React.CSSProperties => ({ width: 8, height: 8, borderRadius: '50%', flexShrink: 0, marginTop: 3, background: c });

            const rows = checks.map((c, i) => (
              <div key={i} style={rowStyle}>
                <span style={dotStyle(dotColor(c.status))} />
                <span style={{ fontWeight: 500, minWidth: 120, flexShrink: 0 }}>{c.check}</span>
                <span style={{ color: 'var(--text-muted)', wordBreak: 'break-all' }}>{c.detail ?? ''}</span>
              </div>
            ));

            const body = (
              <div style={{ background: 'var(--bg-surface)', border: '1px solid var(--border)', borderRadius: 8, overflow: 'hidden', fontSize: 13 }}>
                <div style={{ padding: '8px 12px', background: 'var(--bg-elevated)', borderBottom: '1px solid var(--border)', fontWeight: 600, fontSize: 13, display: 'flex', alignItems: 'center', gap: 8 }}>
                  <span style={{ color: sColor, display: 'inline-flex' }}>{sIcon}</span> Diagnostics: {status}
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
        downloadFile(exported, `chat-export-${Date.now()}.txt`, 'text/plain');
        addToast({ type: 'success', message: 'Chat exported.', duration: 2000, persist: false });
        return true;
      }
      case 'skills': {
        openSettings('skills');
        return true;
      }
      case 'mcp': {
        openSettings('mcp');
        return true;
      }
      case 'rename': {
        const newTitle = input.slice(8).trim(); // everything after "/rename "
        if (newTitle && sessionId) {
          renameSession(sessionId, newTitle)
            .then(() => {
              useSessionStore.getState().updateSessionTitle(sessionId, newTitle, true);
              addToast({ type: 'success', message: `Session renamed to "${newTitle}"`, duration: 2000, persist: false });
            })
            .catch((e: unknown) => {
              addToast({ type: 'error', message: `Could not rename the session: ${toError(e).message}` });
            });
        } else {
          addToast({ type: 'info', message: 'Usage: /rename <new title>', duration: 3000, persist: false });
        }
        return true;
      }
      case 'workflow': {
        if (!sessionId) {
          addToast({ type: 'info', message: 'Open a session first.', duration: 2000, persist: false });
          return true;
        }
        const wfName = input.slice(10).trim().split(/\s+/)[0]; // after "/workflow "
        if (!wfName) {
          addToast({ type: 'info', message: 'Usage: /workflow <saved-name>', duration: 3000, persist: false });
          return true;
        }
        void (async () => {
          // Launch on the composer's CURRENT model and effort, so a saved
          // workflow behaves the same however it was started.
          const settings = useSettingsStore.getState();
          const { selectedModel } = settings;
          const effortLevel = activeEffortLevel(settings);
          try {
            const r = await launchRun({
              name: wfName,
              session_id: sessionId,
              model_key: selectedModel || undefined,
              effort_label: effortLevel || undefined,
            });
            getChatStore(sessionId).getState().addMessage({
              role: 'assistant',
              content: '',
              timestamp: new Date().toISOString(),
              toolUse: [{ toolId: 'workflow_started', toolName: 'workflow_started', input: { run_id: r.run_id, name: wfName }, status: 'complete' }],
              standalone: true,
            });
          } catch (e) {
            // 403 = saved but not trusted; the server refuses to run it without
            // an approval, so say which of the two it is.
            const untrusted = (e as { status?: number })?.status === 403;
            addToast({
              type: 'error',
              message: untrusted
                ? `Workflow '${wfName}' needs one approval first: run it from chat and approve its preview card.`
                : `Could not run workflow '${wfName}': ${toError(e).message}`,
              duration: 4000,
            });
          }
        })();
        return true;
      }
      case 'ci': {
        if (!sessionId) {
          addToast({ type: 'info', message: 'Open a session first.', duration: 2000, persist: false });
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
                  standalone: true,
                });
              }
              if (plan.script) {
                chat.addMessage({
                  role: 'assistant', content: '', timestamp: new Date().toISOString(),
                  // effort_label and workspace_path ride the card for the same
                  // reason model_id does: without them the approved fix run
                  // launches at no effort, or against whatever is connected by
                  // the time the user clicks Approve instead of the repo the
                  // plan was actually diagnosed against.
                  toolUse: [{ toolId: 'workflow_preview', toolName: 'workflow_preview', input: { script: plan.script, name: 'ci-autofix', description: plan.summary, phases: [{ title: 'Fix' }, { title: 'Verify' }], budget_tokens: plan.budget_tokens ?? null, model_id: modelId, effort_label: activeEffortLevel(s), workspace_path: plan.cwd }, status: 'complete' }],
                  standalone: true,
                });
              } else {
                addToast({ type: 'info', message: plan.summary, duration: 4000 });
              }
            } else {
              const r = await startWatch({ branch: sub || undefined, session_id: sessionId });
              getChatStore(sessionId).getState().addMessage({
                role: 'assistant', content: '', timestamp: new Date().toISOString(),
                toolUse: [{ toolId: 'ci_started', toolName: 'ci_started', input: { task_id: r.task_id, branch: r.branch }, status: 'complete' }],
                standalone: true,
              });
            }
          } catch (e) {
            const reason = e instanceof Error ? e.message : String(e);
            addToast({ type: 'error', message: `CI failed: ${reason}`, duration: 6000 });
          }
        })();
        return true;
      }
      case 'goal': {
        if (!sessionId) {
          addToast({ type: 'info', message: 'Open a session first.', duration: 2000, persist: false });
          return true;
        }
        const goalText = input.slice(6).trim(); // everything after "/goal "
        const gateMatch = /^gate(?:\s+(\S+))?(?:\s+([\s\S]+))?$/i.exec(goalText);
        if (gateMatch) {
          // Quality gates: shell commands that must exit 0 before the goal can
          // be judged done. They run at every turn boundary, before the judge.
          const sub = (gateMatch[1] || 'list').toLowerCase();
          const rest = (gateMatch[2] || '').trim();
          const base = `/api/sessions/${sessionId}/goal/gates`;
          const showGates = (gates: Array<{ command: string; failures: number }>) => {
            const lines = gates.length
              ? gates.map((g, i) => `${i + 1}. ${g.command}${g.failures ? ` (failed ${g.failures}x)` : ''}`).join('\n')
              : 'No quality gates. Add one with /goal gate add <command>.';
            addToast({ type: 'info', message: lines, duration: 6000, persist: false });
          };
          void (async () => {
            try {
              if (sub === 'add') {
                if (!rest) {
                  addToast({ type: 'info', message: 'Usage: /goal gate add <shell command>', duration: 3000, persist: false });
                  return;
                }
                const r = await fetch(base, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: rest }) });
                const data = await r.json();
                addToast({ type: 'success', message: `Gate added. ${data.gates?.length ?? 0} gate(s) must exit 0 before the goal counts as done.`, duration: 3000, persist: false });
              } else if (sub === 'remove') {
                const idx = parseInt(rest, 10);
                if (!idx) {
                  addToast({ type: 'info', message: 'Usage: /goal gate remove <N>', duration: 3000, persist: false });
                  return;
                }
                const r = await fetch(`${base}/${idx}`, { method: 'DELETE' });
                const data = await r.json();
                showGates(data.gates ?? []);
              } else if (sub === 'clear') {
                await fetch(base, { method: 'DELETE' });
                addToast({ type: 'success', message: 'All quality gates removed.', duration: 2000, persist: false });
              } else {
                const r = await fetch(base);
                const data = await r.json();
                showGates(data.gates ?? []);
              }
            } catch {
              addToast({ type: 'error', message: 'Could not update quality gates.', duration: 3000, persist: false });
            }
          })();
        } else if (goalText.toLowerCase() === 'clear') {
          void (async () => {
            try {
              await clearSessionGoal(sessionId);
            } catch (e) {
              addToast({ type: 'error', message: `Could not clear the goal: ${toError(e).message}` });
              return;
            }
            useGoalStore.getState().clearGoal(sessionId);
            addToast({ type: 'success', message: 'Goal cleared.', duration: 2000, persist: false });
          })();
        } else if (!goalText) {
          addToast({ type: 'info', message: 'Usage: /goal <text>  ·  /goal clear  ·  /goal gate add <command>', duration: 3000, persist: false });
        } else {
          // The goal is armed on the server first: a refused or failed write
          // says why and sends nothing, so no turn runs without its gate.
          void (async () => {
            try {
              await setSessionGoal(sessionId, goalText);
            } catch (e) {
              addToast({ type: 'error', message: `Could not set the goal: ${toError(e).message}` });
              return;
            }
            useGoalStore.getState().setGoal(sessionId, goalText, true);
            // Claude Code semantics: the goal text IS the first message, so
            // arming the gate also starts the work. If a turn is already
            // streaming, fold it into that turn instead of starting a second.
            if (getChatStore(sessionId).getState().isStreaming) {
              void chatStream.sendMidTurn(goalText);
              addToast({ type: 'success', message: 'Goal set. Steering the running turn toward it.', duration: 2500, persist: false });
            } else {
              void chatStream.send(goalText);
              addToast({ type: 'success', message: 'Goal set. Working toward it now.', duration: 2500, persist: false });
            }
          })();
        }
        return true;
      }
      case 'memory': {
        if (arg === 'on') {
          setAutoMemory(true);
          addToast({ type: 'success', message: 'Memory enabled', duration: 2000, persist: false });
        } else if (arg === 'off') {
          setAutoMemory(false);
          addToast({ type: 'success', message: 'Memory disabled', duration: 2000, persist: false });
        } else if (arg === 'status') {
          addToast({ type: 'info', message: `Memory is ${autoMemory ? 'enabled' : 'disabled'}`, duration: 2000, persist: false });
        } else {
          addToast({ type: 'info', message: 'Usage: /memory on|off|status', duration: 3000, persist: false });
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
                <span style={{ flexShrink: 0, width: 24, display: 'inline-flex', justifyContent: 'center', color: 'var(--text-muted)' }}>{ICON[c.icon]}</span>
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
      case 'compact': {
        if (sessionId) {
          const note = input.trim().slice('/compact'.length).trim();
          void runCompact(sessionId, note, selectedModel, addToast);
        }
        return true;
      }
      case 'clear': {
        if (sessionId) void clearConversation(sessionId);
        addToast({ type: 'info', message: 'Chat cleared', duration: 2000, persist: false });
        return true;
      }
      case 'settings': {
        openSettings('general');
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
          addToast({ type: 'info', message: 'Notifications are already enabled', duration: 2000, persist: false });
        } else if (Notification.permission === 'denied') {
          addToast({ type: 'warning', message: 'Notifications blocked. Reset site permissions in browser settings.', duration: 4000 });
        } else {
          void Notification.requestPermission().then(perm => {
            if (perm === 'granted') addToast({ type: 'success', message: 'Notifications enabled', duration: 2000, persist: false });
            else addToast({ type: 'warning', message: 'Notifications blocked', duration: 3000 });
          });
        }
        return true;
      }
      case 'plan': {
        void handlePlanToggle();
        return true;
      }
      default:
        return false;
    }
  }, [setEffortLevel, setTheme, openSettings, addToast, selectedModel, autoMemory, setAutoMemory, sessionId, handleNativeBrowse, handlePlanToggle, chatStream, SLASH_COMMANDS, models]);

  return { handleSlashCommand };
}
