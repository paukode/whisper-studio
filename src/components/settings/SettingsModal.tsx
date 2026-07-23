import { useRef } from 'react';
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
import { AutoModePanel } from './AutoModePanel';
import { ModelModePanel } from './ModelModePanel';
import { FeatureFlagsPanel } from './FeatureFlagsPanel';
import { PreviewSettings } from './PreviewSettings';

export type SettingsTabId =
  | 'apikeys'
  | 'model-mode'
  | 'feature-flags'
  | 'skills'
  | 'mcp'
  | 'permissions'
  | 'hooks'
  | 'cron'
  | 'auto-mode'
  | 'plugins'
  | 'stats'
  | 'costs'
  | 'preview';

interface SettingsTab {
  id: SettingsTabId;
  label: string;
}

const TABS: SettingsTab[] = [
  { id: 'apikeys', label: 'API Keys' },
  { id: 'model-mode', label: 'Model Mode' },
  { id: 'feature-flags', label: 'Feature Flags' },
  { id: 'skills', label: 'Skills' },
  { id: 'mcp', label: 'MCP' },
  { id: 'permissions', label: 'Permissions' },
  { id: 'hooks', label: 'Hooks' },
  { id: 'cron', label: 'Scheduled Tasks' },
  { id: 'auto-mode', label: 'Auto Mode' },
  { id: 'plugins', label: 'Plugins' },
  { id: 'stats', label: 'Stats' },
  { id: 'costs', label: 'Costs' },
  { id: 'preview', label: 'Live Preview' },
];

/**
 * Map tab IDs to their panel components.
 */
const TAB_COMPONENTS: Record<SettingsTabId, React.FC> = {
  apikeys: APISettings,
  'model-mode': ModelModePanel,
  'feature-flags': FeatureFlagsPanel,
  skills: SkillsPanel,
  mcp: MCPSettings,
  permissions: PermissionsPanel,
  hooks: HooksPanel,
  cron: CronPanel,
  'auto-mode': AutoModePanel,
  plugins: PluginsPanel,
  stats: StatsPanel,
  costs: CostsPanel,
  preview: PreviewSettings,
};

export const SettingsModal: React.FC = () => {
  const settingsOpen = useUIStore((s) => s.settingsOpen);
  const settingsTab = useUIStore((s) => s.settingsTab);
  const closeSettings = useUIStore((s) => s.closeSettings);
  const openSettings = useUIStore((s) => s.openSettings);
  const modalRef = useRef<HTMLDivElement>(null);

  const activeTabId = (TABS.some((t) => t.id === settingsTab)
    ? settingsTab
    : 'apikeys') as SettingsTabId;

  const ActiveTabComponent = TAB_COMPONENTS[activeTabId];

  // Escape closes; outside-click is already handled by the overlay onClick.
  // The focus trap keeps Tab inside and restores focus to the trigger
  // (e.g. the header gear) on close — it owns initial focus too.
  useDismiss(modalRef, closeSettings, { enabled: settingsOpen, outsideClick: false });
  useFocusTrap(modalRef, settingsOpen);

  if (!settingsOpen) return null;

  return (
    <div
      className="settings-overlay"
      onClick={closeSettings}
      role="presentation"
    >
      <div
        className="settings-container"
        ref={modalRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="settings-title"
        onClick={(e) => e.stopPropagation()}
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

        <nav className="settings-tabs" role="tablist" aria-label="Settings tabs">
          {TABS.map((tab) => (
            <button
              key={tab.id}
              role="tab"
              aria-selected={activeTabId === tab.id}
              aria-controls={`settings-panel-${tab.id}`}
              className={`settings-tab${activeTabId === tab.id ? ' active' : ''}`}
              onClick={() => openSettings(tab.id)}
              type="button"
            >
              {tab.label}
            </button>
          ))}
        </nav>

        <div
          className="settings-body"
          role="tabpanel"
          id={`settings-panel-${activeTabId}`}
          aria-labelledby={`settings-tab-${activeTabId}`}
        >
          <ActiveTabComponent />
        </div>
      </div>
    </div>
  );
};
