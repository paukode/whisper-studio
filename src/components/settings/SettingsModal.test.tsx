import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SettingsModal } from './SettingsModal';
import { useUIStore } from '@/stores/uiStore';

// The modal renders one tab panel at a time; every panel talks to the backend
// through TanStack Query. Stub them all so this suite exercises only the modal
// shell's dismissal behaviour, not any panel's data-fetching. Factories are
// hoisted above imports, so each must be self-contained (no shared helper).
vi.mock('./APISettings', () => ({ APISettings: () => <div>APISettings</div> }));
vi.mock('./MCPSettings', () => ({ MCPSettings: () => <div>MCPSettings</div> }));
vi.mock('./ImportPanel', () => ({ ImportPanel: () => <div>ImportPanel</div> }));
vi.mock('./SkillsPanel', () => ({ SkillsPanel: () => <div>SkillsPanel</div> }));
vi.mock('./PermissionsPanel', () => ({ PermissionsPanel: () => <div>PermissionsPanel</div> }));
vi.mock('./CostsPanel', () => ({ CostsPanel: () => <div>CostsPanel</div> }));
vi.mock('./HooksPanel', () => ({ HooksPanel: () => <div>HooksPanel</div> }));
vi.mock('./CronPanel', () => ({ CronPanel: () => <div>CronPanel</div> }));
vi.mock('./PluginsPanel', () => ({ PluginsPanel: () => <div>PluginsPanel</div> }));
vi.mock('./StatsPanel', () => ({ StatsPanel: () => <div>StatsPanel</div> }));
vi.mock('./ModelModePanel', () => ({ ModelModePanel: () => <div>ModelModePanel</div> }));
vi.mock('./ModelsPanel', () => ({ ModelsPanel: () => <div>ModelsPanel</div> }));
vi.mock('./FeatureFlagsPanel', () => ({ FeatureFlagsPanel: () => <div>FeatureFlagsPanel</div> }));
vi.mock('./PreviewSettings', () => ({ PreviewSettings: () => <div>PreviewSettings</div> }));
vi.mock('./WorkflowsPanel', () => ({ WorkflowsPanel: () => <div>WorkflowsPanel</div> }));

const isOpen = () => useUIStore.getState().settingsOpen;

beforeEach(() => {
  useUIStore.setState({ settingsOpen: true, settingsTab: 'apikeys' });
});

describe('SettingsModal — dismissal', () => {
  it('does NOT close on a backdrop (overlay) click', () => {
    const { container } = render(<SettingsModal />);
    const overlay = container.querySelector('.settings-overlay') as HTMLElement;
    expect(overlay).toBeTruthy();

    // A plain click on the dark backdrop must be inert.
    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(isOpen()).toBe(true);
    expect(screen.getByRole('dialog')).toBeInTheDocument();
  });

  it('does NOT close when a selection drag starts inside and releases on the backdrop', () => {
    const { container } = render(<SettingsModal />);
    const overlay = container.querySelector('.settings-overlay') as HTMLElement;
    const contentPanel = screen.getByText('APISettings');

    // Press begins on the content; the resulting click (on release outside)
    // targets the overlay — the historical "select text, release outside" bug.
    fireEvent.mouseDown(contentPanel);
    fireEvent.click(overlay);

    expect(isOpen()).toBe(true);
  });

  it('does NOT close on mouseLeave of the window/overlay', () => {
    const { container } = render(<SettingsModal />);
    const overlay = container.querySelector('.settings-overlay') as HTMLElement;
    const modal = screen.getByRole('dialog');

    fireEvent.mouseLeave(modal);
    fireEvent.mouseLeave(overlay);

    expect(isOpen()).toBe(true);
  });

  it('DOES close on the × button', () => {
    render(<SettingsModal />);
    fireEvent.click(screen.getByRole('button', { name: 'Close settings' }));
    expect(isOpen()).toBe(false);
  });

  it('DOES close on Escape (kept as a deliberate, explicit gesture)', () => {
    render(<SettingsModal />);
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(isOpen()).toBe(false);
  });
});

describe('SettingsModal — deep-link routing', () => {
  const openAt = (tab: string) => {
    useUIStore.setState({ settingsOpen: true, settingsTab: tab });
    return render(<SettingsModal />);
  };

  it('old id "skills" opens Tools and automation → Skills', () => {
    openAt('skills');
    // The merged panel renders under its sub-tab strip.
    expect(screen.getByText('SkillsPanel')).toBeInTheDocument();
    // The rail item that owns it is the active section.
    expect(
      screen.getByRole('button', { name: /Skills and plugins/i }),
    ).toHaveAttribute('aria-current', 'page');
    // The Skills sub-tab is the selected tab.
    expect(screen.getByRole('tab', { name: 'Skills' })).toHaveAttribute('aria-selected', 'true');
  });

  it('old id "costs" opens Usage and advanced → Costs', () => {
    openAt('costs');
    expect(screen.getByText('CostsPanel')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Usage and advanced/i }),
    ).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('tab', { name: 'Costs' })).toHaveAttribute('aria-selected', 'true');
  });

  it('old id "hooks" opens Workflows and tasks → Hooks', () => {
    openAt('hooks');
    expect(screen.getByText('HooksPanel')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Workflows and tasks/i }),
    ).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('tab', { name: 'Hooks' })).toHaveAttribute('aria-selected', 'true');
  });

  it('old id "apikeys" opens Keys and permissions → API keys', () => {
    openAt('apikeys');
    expect(screen.getByText('APISettings')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Keys and permissions/i }),
    ).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('tab', { name: 'API keys' })).toHaveAttribute('aria-selected', 'true');
  });

  it('old id "mcp" opens the single MCP servers panel with no sub-tab strip', () => {
    openAt('mcp');
    expect(screen.getByText('MCPSettings')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /MCP servers/i })).toHaveAttribute(
      'aria-current',
      'page',
    );
    // Single-panel rail items render no sub-tabs.
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
  });

  it('new id "import" opens the single Import project panel with no sub-tab strip', () => {
    openAt('import');
    expect(screen.getByText('ImportPanel')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Import project/i })).toHaveAttribute(
      'aria-current',
      'page',
    );
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
  });

  it('an unknown tab id falls back to Keys and permissions → API keys', () => {
    openAt('general'); // the store's initial default value
    expect(screen.getByText('APISettings')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Keys and permissions/i }),
    ).toHaveAttribute('aria-current', 'page');
  });

  it('clicking a rail item then a sub-tab swaps the visible panel', () => {
    openAt('apikeys');
    // Jump to the Skills and plugins section via the rail.
    fireEvent.click(screen.getByRole('button', { name: /Skills and plugins/i }));
    expect(screen.getByText('SkillsPanel')).toBeInTheDocument();
    // Switch to the Plugins sub-tab.
    fireEvent.click(screen.getByRole('tab', { name: 'Plugins' }));
    expect(screen.getByText('PluginsPanel')).toBeInTheDocument();
    expect(screen.getByRole('tab', { name: 'Plugins' })).toHaveAttribute('aria-selected', 'true');
  });
});
