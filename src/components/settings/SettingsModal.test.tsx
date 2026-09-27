import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SettingsModal } from './SettingsModal';
import { useUIStore } from '@/stores/uiStore';
import { STORAGE_KEYS } from '@/utils/storageKeys';

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
vi.mock('./ModelModePanel', () => ({ ModelModePanel: () => <div>ModelModePanel</div> }));
vi.mock('./ModelsPanel', () => ({ ModelsPanel: () => <div>ModelsPanel</div> }));
vi.mock('./FeatureFlagsPanel', () => ({ FeatureFlagsPanel: () => <div>FeatureFlagsPanel</div> }));
vi.mock('./PreviewSettings', () => ({ PreviewSettings: () => <div>PreviewSettings</div> }));

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

  it('the retired id "stats" is not routed: it opens the default, API keys', () => {
    // Stats was folded into Costs; no alias keeps the old id alive.
    openAt('stats');
    expect(screen.getByText('APISettings')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Keys and permissions/i }),
    ).toHaveAttribute('aria-current', 'page');
  });

  it('Usage and advanced opens on Costs and has no Stats sub-tab', () => {
    openAt('usage-advanced');
    expect(screen.getAllByRole('tab')[0]).toHaveTextContent('Costs');
    expect(screen.getByText('CostsPanel')).toBeInTheDocument();
    expect(screen.queryByRole('tab', { name: 'Stats' })).toBeNull();
  });

  it('old id "hooks" opens Tasks and hooks → Hooks', () => {
    openAt('hooks');
    expect(screen.getByText('HooksPanel')).toBeInTheDocument();
    expect(
      screen.getByRole('button', { name: /Tasks and hooks/i }),
    ).toHaveAttribute('aria-current', 'page');
    expect(screen.getByRole('tab', { name: 'Hooks' })).toHaveAttribute('aria-selected', 'true');
  });

  it('has no Workflows destination anywhere in Settings', () => {
    // Removed by user request: workflows are approved and managed from chat.
    openAt('hooks');
    expect(screen.queryByText(/^Workflows$/)).not.toBeInTheDocument();
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

describe('SettingsModal: resizing', () => {
  const saved = () => JSON.parse(localStorage.getItem(STORAGE_KEYS.SETTINGS_GEOMETRY)!);
  const dragFrom = (el: Element, dx: number, dy: number) => {
    fireEvent.pointerDown(el, { clientX: 500, clientY: 400 });
    fireEvent.pointerMove(window, { clientX: 500 + dx, clientY: 400 + dy });
    fireEvent.pointerUp(window);
  };

  beforeEach(() => localStorage.clear());

  it('resizes from its edges, remembers the size, and a header double-click puts it back', () => {
    const { container } = render(<SettingsModal />);
    const dialog = screen.getByRole('dialog');
    expect(container.querySelectorAll('.settings-container > .ws-rz')).toHaveLength(8);
    expect(dialog.style.width).toBe('960px');

    dragFrom(container.querySelector('.ws-rz-e')!, -100, 0);
    expect(dialog.style.width).toBe('860px');
    expect(saved()).toMatchObject({ w: 860 });

    fireEvent.doubleClick(container.querySelector('.settings-header')!);
    expect(dialog.style.width).toBe('960px');
    expect(saved()).toEqual({ w: 960, h: null, x: 0, y: 0 });
  });

  it('moves by its header, but a press on Close never drags it', () => {
    render(<SettingsModal />);
    dragFrom(screen.getByRole('button', { name: 'Close settings' }), 100, 60);
    expect(saved()).toMatchObject({ x: 0, y: 0 });

    // jsdom's window is 1024 wide, so a 960px dialog has 16px of room either way.
    dragFrom(screen.getByRole('heading', { name: 'Settings' }), 12, 60);
    expect(saved()).toMatchObject({ x: 12, y: 60 });
  });
});
