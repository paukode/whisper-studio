import React, { useEffect, useMemo, useRef, useState } from 'react';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useTheme } from '@/providers/ThemeProvider';
import { useDismiss } from '@/hooks/useDismiss';
import { requestModelChange } from '@/components/chat/dataRetentionConsent';
import { effortLabel } from '@/utils/effort';
import { PERMISSION_MODES } from '@/utils/permissionModes';
import { put } from '@/api/client';

/**
 * ⌘K / Ctrl+K command palette — the connective tissue the app was missing.
 * A searchable list over actions that already exist in the stores, so every
 * capability (new session, connect workspace, switch model/mode/theme, open
 * a settings tab, toggle panels) is reachable without hunting for its button.
 *
 * Purely additive: opens from a uiStore flag, runs existing store actions,
 * and dismisses via the shared useDismiss hook. Model switches route through
 * requestModelChange so the Fable-5 retention gate still applies.
 */

interface Command {
  id: string;
  title: string;
  group: string;
  keywords?: string;
  run: () => void;
}

export const CommandPalette: React.FC = () => {
  const open = useUIStore((s) => s.commandPaletteOpen);
  const close = useUIStore((s) => s.closeCommandPalette);
  const models = useSettingsStore((s) => s.models);
  const selectedModel = useSettingsStore((s) => s.selectedModel);
  const { themes, setTheme } = useTheme();

  const panelRef = useRef<HTMLDivElement>(null);
  const listRef = useRef<HTMLDivElement>(null);
  const [query, setQuery] = useState('');
  const [active, setActive] = useState(0);

  useDismiss(panelRef, close, { enabled: open, outsideClick: true });

  // Keep the highlighted row visible as the user arrows through a long list.
  useEffect(() => {
    listRef.current?.children[active]?.scrollIntoView({ block: 'nearest' });
  }, [active]);

  const commands = useMemo<Command[]>(() => {
    const ui = useUIStore.getState();
    const settings = useSettingsStore.getState();
    const run = (fn: () => void) => () => { close(); fn(); };

    const list: Command[] = [
      { id: 'new-session', group: 'Session', title: 'New conversation', keywords: 'chat start',
        run: run(() => useSessionStore.getState().createSession()) },
      { id: 'connect-ws', group: 'Workspace', title: 'Connect a workspace', keywords: 'folder open project',
        run: run(() => ui.openWorkspaceConnect()) },
      { id: 'toggle-transcript', group: 'View', title: 'Toggle transcript panel', keywords: 'transcribe record',
        run: run(() => ui.toggleTranscript()) },
      { id: 'toggle-sidebar', group: 'View', title: 'Toggle sessions sidebar', keywords: 'conversations list',
        run: run(() => ui.toggleSidebar()) },
      { id: 'edit-memory', group: 'Workspace', title: 'Edit project memory (WHISPER.md)', keywords: 'instructions notes',
        run: run(() => ui.openMemoryEditor()) },
    ];

    // Settings tabs
    const tabs: Array<[string, string]> = [
      ['apikeys', 'API Keys'], ['skills', 'Skills'], ['mcp', 'MCP'],
      ['permissions', 'Permissions'], ['hooks', 'Hooks'], ['cron', 'Cron'],
      ['auto-mode', 'Auto Mode'], ['plugins', 'Plugins'], ['stats', 'Stats'], ['costs', 'Costs'],
    ];
    for (const [id, label] of tabs) {
      list.push({ id: `settings-${id}`, group: 'Settings', title: `Settings: ${label}`, keywords: 'preferences config',
        run: run(() => ui.openSettings(id)) });
    }

    // Models — routed through the retention gate.
    for (const m of models) {
      list.push({ id: `model-${m.key}`, group: 'Model', title: `Switch model: ${m.name}`, keywords: 'llm',
        run: run(() => { void requestModelChange(m.key); }) });
    }

    // Permission modes. Same write path as ChatInput's Mode dropdown
    // (handleSelectMode): flip the Plan flag, update the config's permissionMode,
    // then PUT /api/permissions/mode so the backend session_approvals view stays
    // in sync. Previously these just opened the Settings tab instead of applying.
    for (const { value, label } of PERMISSION_MODES) {
      list.push({ id: `mode-${label}`, group: 'Mode', title: `Set permission mode: ${label}`, keywords: 'permission',
        run: run(() => {
          settings.setPlanMode(value === 'plan');
          settings.updateConfig({ permissionMode: value });
          void put('/api/permissions/mode', { mode: value }).catch((err) => {
            console.warn('Failed to set permission mode:', err);
          });
        }) });
    }

    // Effort levels — only those the selected model supports.
    const effortLevels = models.find((m) => m.key === selectedModel)?.effort_levels ?? [];
    for (const lvl of effortLevels) {
      list.push({ id: `effort-${lvl}`, group: 'Effort', title: `Set effort: ${effortLabel(lvl)}`, keywords: 'thinking depth',
        run: run(() => settings.setEffortLevel(lvl)) });
    }

    // Themes — keep keywords generic ("appearance") so the distinguishing
    // word (Light/Dark/Contrast) comes from the label, not a shared keyword
    // that would make every theme match "light".
    for (const t of themes) {
      list.push({ id: `theme-${t.key}`, group: 'Theme', title: `Theme: ${t.label}`, keywords: 'appearance color',
        run: run(() => setTheme(t.key)) });
    }

    return list;
  }, [models, selectedModel, themes, setTheme, close]);

  const filtered = useMemo(() => {
    const tokens = query.trim().toLowerCase().split(/\s+/).filter(Boolean);
    if (tokens.length === 0) return commands;
    // Every whitespace-separated token must appear somewhere in the
    // command's searchable text, so "theme light" matches "Theme: Light".
    return commands.filter((c) => {
      const hay = `${c.title} ${c.group} ${c.keywords ?? ''}`.toLowerCase();
      return tokens.every((t) => hay.includes(t));
    });
  }, [commands, query]);

  if (!open) return null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (e.key === 'ArrowDown') { e.preventDefault(); setActive((i) => Math.min(i + 1, filtered.length - 1)); }
    else if (e.key === 'ArrowUp') { e.preventDefault(); setActive((i) => Math.max(i - 1, 0)); }
    else if (e.key === 'Enter') { e.preventDefault(); filtered[active]?.run(); }
    else if (e.key === 'Escape') { e.preventDefault(); close(); }
  };

  return (
    <div className="cmdk-overlay" role="presentation">
      <div ref={panelRef} className="cmdk-panel" role="dialog" aria-modal="true" aria-label="Command palette">
        <div className="cmdk-input-row">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.5" y2="16.5" />
          </svg>
          <input
            className="cmdk-input"
            autoFocus
            placeholder="Type a command or search…"
            value={query}
            onChange={(e) => { setQuery(e.target.value); setActive(0); }}
            onKeyDown={onKeyDown}
          />
          <kbd className="cmdk-esc">esc</kbd>
        </div>
        <div className="cmdk-list" ref={listRef}>
          {filtered.length === 0 ? (
            <div className="cmdk-empty">No matching commands</div>
          ) : (
            filtered.map((c, i) => (
              <button
                key={c.id}
                type="button"
                className={`cmdk-item${i === active ? ' active' : ''}`}
                onMouseEnter={() => setActive(i)}
                onClick={() => c.run()}
              >
                <span className="cmdk-item-title">{c.title}</span>
                <span className="cmdk-item-group">{c.group}</span>
              </button>
            ))
          )}
        </div>
      </div>
    </div>
  );
};
