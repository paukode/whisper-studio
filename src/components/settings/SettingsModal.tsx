import { useCallback, useMemo, useRef } from 'react';
import { useUIStore } from '@/stores/uiStore';
import { useDismiss, useFocusTrap } from '@/hooks/useDismiss';
import { APISettings } from './APISettings';
import { MCPSettings } from './MCPSettings';
import { SkillsPanel } from './SkillsPanel';
import { PermissionsPanel } from './PermissionsPanel';
import { CostsPanel } from './CostsPanel';
import { HooksPanel } from './HooksPanel';
import { CronPanel } from './CronPanel';
import { PluginsPanel } from './PluginsPanel';
import { StatsPanel } from './StatsPanel';
import { ModelModePanel } from './ModelModePanel';
import { ModelsPanel } from './ModelsPanel';
import { FeatureFlagsPanel } from './FeatureFlagsPanel';
import { PreviewSettings } from './PreviewSettings';
import { WorkflowsPanel } from './WorkflowsPanel';

/** The original 14 flat tab ids. They stay valid `settingsTab` values forever:
 *  every `openSettings(<id>)` caller (ChatPanel, MoreMenu, the command palette,
 *  slash commands, tests) keeps working because {@link TAB_ROUTE} maps each one
 *  to its new rail item + sub-tab. */
export type SettingsTabId =
  | 'apikeys'
  | 'model-mode'
  | 'models'
  | 'feature-flags'
  | 'skills'
  | 'workflows'
  | 'mcp'
  | 'permissions'
  | 'hooks'
  | 'cron'
  | 'plugins'
  | 'stats'
  | 'costs'
  | 'preview';

interface SettingsTab {
  id: SettingsTabId;
  label: string;
}

/** The flat destination list. The command palette derives its "Settings: …"
 *  entries from this, so every one of these ids still resolves to a real place
 *  in the dialog (via {@link TAB_ROUTE}) and stays reachable in one click. */
export const SETTINGS_TABS: SettingsTab[] = [
  { id: 'apikeys', label: 'API keys' },
  { id: 'model-mode', label: 'Model mode' },
  { id: 'models', label: 'Models' },
  { id: 'feature-flags', label: 'Feature flags' },
  { id: 'skills', label: 'Skills' },
  { id: 'workflows', label: 'Workflows' },
  { id: 'mcp', label: 'MCP servers' },
  { id: 'permissions', label: 'Permissions' },
  { id: 'hooks', label: 'Hooks' },
  { id: 'cron', label: 'Scheduled tasks' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'stats', label: 'Stats' },
  { id: 'costs', label: 'Costs' },
  { id: 'preview', label: 'Live preview' },
];

/* ────────────────────────────────────────────────────────────────────────
   Grouped left rail. Each rail item renders one or more EXISTING panels under
   a secondary sub-tab strip; nothing about a panel's internals changes — the
   restructure only groups and gates which panel shows. Single-panel items
   (Model mode, MCP servers) render their one panel with no sub-tab strip.
   ──────────────────────────────────────────────────────────────────────── */

type RailItemId =
  | 'model-mode'
  | 'models'
  | 'mcp'
  | 'skills-plugins'
  | 'workflows-tasks'
  | 'keys-permissions'
  | 'usage-advanced';

interface SubTab {
  /** A `settingsTab` value: deep-linking to this id opens this exact sub-tab. */
  id: string;
  label: string;
  render: () => React.ReactNode;
}

interface RailItem {
  id: RailItemId;
  label: string;
  icon: React.ReactNode;
  subTabs: SubTab[];
}

interface RailGroup {
  label: string;
  items: RailItem[];
}

/* Small stroke icons in the app's line-art style (16px, currentColor). */
const icon = (paths: React.ReactNode): React.ReactNode => (
  <svg
    width="16"
    height="16"
    viewBox="0 0 24 24"
    fill="none"
    stroke="currentColor"
    strokeWidth="2"
    strokeLinecap="round"
    strokeLinejoin="round"
    aria-hidden="true"
  >
    {paths}
  </svg>
);

const RAIL_GROUPS: RailGroup[] = [
  {
    label: 'Chat and models',
    items: [
      {
        id: 'model-mode',
        label: 'Model mode',
        icon: icon(<><circle cx="12" cy="12" r="3" /><path d="M12 3v2M12 19v2M3 12h2M19 12h2M5.6 5.6l1.4 1.4M17 17l1.4 1.4M18.4 5.6L17 7M7 17l-1.4 1.4" /></>),
        subTabs: [{ id: 'model-mode', label: 'Model mode', render: () => <ModelModePanel /> }],
      },
      {
        id: 'models',
        label: 'Models',
        icon: icon(<><path d="M21 16V8a2 2 0 0 0-1-1.73l-7-4a2 2 0 0 0-2 0l-7 4A2 2 0 0 0 3 8v8a2 2 0 0 0 1 1.73l7 4a2 2 0 0 0 2 0l7-4A2 2 0 0 0 21 16z" /><path d="M3.27 6.96L12 12l8.73-5.04M12 22V12" /></>),
        subTabs: [
          { id: 'models', label: 'Chat', render: () => <ModelsPanel section="chat" /> },
          { id: 'models-discover', label: 'Discover', render: () => <ModelsPanel section="discover" /> },
          { id: 'models-transcription', label: 'Transcription', render: () => <ModelsPanel section="transcription" /> },
          { id: 'models-indexing', label: 'Indexing', render: () => <ModelsPanel section="indexing" /> },
        ],
      },
    ],
  },
  {
    label: 'Tools and automation',
    items: [
      {
        id: 'mcp',
        label: 'MCP servers',
        icon: icon(<><rect x="3" y="3" width="7" height="7" rx="1" /><rect x="14" y="3" width="7" height="7" rx="1" /><rect x="3" y="14" width="7" height="7" rx="1" /><rect x="14" y="14" width="7" height="7" rx="1" /></>),
        subTabs: [{ id: 'mcp', label: 'MCP servers', render: () => <MCPSettings /> }],
      },
      {
        id: 'skills-plugins',
        label: 'Skills and plugins',
        icon: icon(<><path d="M12 2l2.4 4.9 5.4.8-3.9 3.8.9 5.4L12 14.8 7.2 17.3l.9-5.4L4.2 8.1l5.4-.8z" /></>),
        subTabs: [
          { id: 'skills', label: 'Skills', render: () => <SkillsPanel /> },
          { id: 'plugins', label: 'Plugins', render: () => <PluginsPanel /> },
        ],
      },
      {
        id: 'workflows-tasks',
        label: 'Workflows and tasks',
        icon: icon(<><path d="M4 4h6v6H4zM14 14h6v6h-6z" /><path d="M10 7h4a2 2 0 0 1 2 2v5" /></>),
        subTabs: [
          { id: 'workflows', label: 'Workflows', render: () => <WorkflowsPanel /> },
          { id: 'cron', label: 'Scheduled tasks', render: () => <CronPanel /> },
          { id: 'hooks', label: 'Hooks', render: () => <HooksPanel /> },
        ],
      },
    ],
  },
  {
    label: 'Access and usage',
    items: [
      {
        id: 'keys-permissions',
        label: 'Keys and permissions',
        icon: icon(<><circle cx="7.5" cy="15.5" r="4.5" /><path d="M10.7 12.3L20 3M17 6l2 2M14 9l2 2" /></>),
        subTabs: [
          { id: 'apikeys', label: 'API keys', render: () => <APISettings /> },
          { id: 'permissions', label: 'Permissions', render: () => <PermissionsPanel /> },
        ],
      },
      {
        id: 'usage-advanced',
        label: 'Usage and advanced',
        icon: icon(<><path d="M3 3v18h18" /><path d="M7 14l3-3 3 3 5-6" /></>),
        subTabs: [
          { id: 'stats', label: 'Stats', render: () => <StatsPanel /> },
          { id: 'costs', label: 'Costs', render: () => <CostsPanel /> },
          { id: 'feature-flags', label: 'Feature flags', render: () => <FeatureFlagsPanel /> },
          { id: 'preview', label: 'Live preview', render: () => <PreviewSettings /> },
        ],
      },
    ],
  },
];

const ALL_RAIL_ITEMS: RailItem[] = RAIL_GROUPS.flatMap((g) => g.items);

/** Resolve any `settingsTab` value to its rail item + sub-tab. Built from the
 *  rail so it can never drift: every rail id and every sub-tab id (which
 *  include all 14 legacy ids) is routable. Unknown values fall back to the
 *  first destination — API keys — preserving the pre-restructure default. */
export const TAB_ROUTE: Record<string, { rail: RailItemId; sub: string }> = (() => {
  const map: Record<string, { rail: RailItemId; sub: string }> = {};
  for (const item of ALL_RAIL_ITEMS) {
    // The rail id itself lands on the item's first sub-tab.
    map[item.id] = { rail: item.id, sub: item.subTabs[0].id };
    for (const st of item.subTabs) {
      map[st.id] = { rail: item.id, sub: st.id };
    }
  }
  return map;
})();

const DEFAULT_ROUTE = TAB_ROUTE['apikeys'];

export const SettingsModal: React.FC = () => {
  const settingsOpen = useUIStore((s) => s.settingsOpen);
  const settingsTab = useUIStore((s) => s.settingsTab);
  const closeSettings = useUIStore((s) => s.closeSettings);
  const openSettings = useUIStore((s) => s.openSettings);
  const modalRef = useRef<HTMLDivElement>(null);
  const railRef = useRef<HTMLElement>(null);
  const subTabsRef = useRef<HTMLDivElement>(null);

  const route = TAB_ROUTE[settingsTab] ?? DEFAULT_ROUTE;
  const activeItem = useMemo(
    () => ALL_RAIL_ITEMS.find((i) => i.id === route.rail) ?? ALL_RAIL_ITEMS[0],
    [route.rail],
  );
  const activeSub =
    activeItem.subTabs.find((st) => st.id === route.sub) ?? activeItem.subTabs[0];
  const hasSubTabs = activeItem.subTabs.length > 1;

  // Escape closes (a deliberate, explicit gesture). The modal does NOT close on
  // a backdrop click, a mouse-leave, or focus loss — the overlay has no dismiss
  // handler — so selecting text and releasing the mouse outside the window can
  // never make Settings disappear. It closes only via the × button or Escape.
  // The focus trap keeps Tab inside and restores focus to the trigger
  // (e.g. the header gear) on close — it owns initial focus too.
  useDismiss(modalRef, closeSettings, { enabled: settingsOpen, outsideClick: false });
  useFocusTrap(modalRef, settingsOpen);

  // Arrow-key roving for the vertical rail (Up/Down) and, when present, the
  // horizontal sub-tab strip (Left/Right + Home/End). Both activate on move,
  // the conventional tab-strip behaviour.
  const onRailKey = useCallback(
    (e: React.KeyboardEvent) => {
      if (e.key !== 'ArrowDown' && e.key !== 'ArrowUp') return;
      e.preventDefault();
      const idx = ALL_RAIL_ITEMS.findIndex((i) => i.id === activeItem.id);
      const next =
        e.key === 'ArrowDown'
          ? (idx + 1) % ALL_RAIL_ITEMS.length
          : (idx - 1 + ALL_RAIL_ITEMS.length) % ALL_RAIL_ITEMS.length;
      openSettings(ALL_RAIL_ITEMS[next].id);
      requestAnimationFrame(() => {
        const btns = railRef.current?.querySelectorAll<HTMLButtonElement>('.settings-rail-item');
        btns?.[next]?.focus();
      });
    },
    [activeItem.id, openSettings],
  );

  const onSubTabKey = useCallback(
    (e: React.KeyboardEvent) => {
      const keys = ['ArrowLeft', 'ArrowRight', 'Home', 'End'];
      if (!keys.includes(e.key)) return;
      e.preventDefault();
      const subs = activeItem.subTabs;
      const idx = subs.findIndex((st) => st.id === activeSub.id);
      let next = idx;
      if (e.key === 'ArrowRight') next = (idx + 1) % subs.length;
      else if (e.key === 'ArrowLeft') next = (idx - 1 + subs.length) % subs.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = subs.length - 1;
      openSettings(subs[next].id);
      requestAnimationFrame(() => {
        const btns = subTabsRef.current?.querySelectorAll<HTMLButtonElement>('.settings-subtab');
        btns?.[next]?.focus();
      });
    },
    [activeItem.subTabs, activeSub.id, openSettings],
  );

  if (!settingsOpen) return null;

  const panelId = `settings-panel-${activeItem.id}-${activeSub.id}`;
  const subTabId = (id: string) => `settings-subtab-${activeItem.id}-${id}`;

  return (
    <div className="settings-overlay" role="presentation">
      <div
        className="settings-container settings-container--rail"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
      >
        <div className="settings-header">
          <h2 id="settings-title">Settings</h2>
          <button
            className="btn-icon settings-close"
            onClick={closeSettings}
            aria-label="Close settings"
            type="button"
          >
            ✕
          </button>
        </div>

        <div className="settings-layout">
          <nav
            className="settings-rail"
            aria-label="Settings sections"
            ref={railRef}
            onKeyDown={onRailKey}
          >
            {RAIL_GROUPS.map((group) => (
              <div className="settings-group" role="group" aria-label={group.label} key={group.label}>
                <div className="settings-group-title" aria-hidden="true">
                  {group.label}
                </div>
                {group.items.map((item) => {
                  const active = item.id === activeItem.id;
                  return (
                    <button
                      key={item.id}
                      type="button"
                      className={`settings-rail-item${active ? ' active' : ''}`}
                      aria-current={active ? 'page' : undefined}
                      onClick={() => openSettings(item.id)}
                    >
                      <span className="settings-rail-icon">{item.icon}</span>
                      <span className="settings-rail-label">{item.label}</span>
                    </button>
                  );
                })}
              </div>
            ))}
          </nav>

          <div className="settings-main">
            {hasSubTabs && (
              <div
                className="settings-subtabs"
                role="tablist"
                aria-label={`${activeItem.label} sections`}
                ref={subTabsRef}
                onKeyDown={onSubTabKey}
              >
                {activeItem.subTabs.map((st) => {
                  const active = st.id === activeSub.id;
                  return (
                    <button
                      key={st.id}
                      id={subTabId(st.id)}
                      type="button"
                      role="tab"
                      aria-selected={active}
                      aria-controls={panelId}
                      tabIndex={active ? 0 : -1}
                      className={`settings-subtab${active ? ' active' : ''}`}
                      onClick={() => openSettings(st.id)}
                    >
                      {st.label}
                    </button>
                  );
                })}
              </div>
            )}

            <div
              className="settings-body"
              role="tabpanel"
              id={panelId}
              aria-labelledby={hasSubTabs ? subTabId(activeSub.id) : undefined}
              aria-label={hasSubTabs ? undefined : activeItem.label}
              tabIndex={0}
            >
              {activeSub.render()}
            </div>
          </div>
        </div>
      </div>
    </div>
  );
};
