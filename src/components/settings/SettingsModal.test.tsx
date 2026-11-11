import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { SettingsModal } from './SettingsModal';
import { useUIStore } from '@/stores/uiStore';
import { STORAGE_KEYS } from '@/utils/storageKeys';

// The modal renders one tab panel at a time; every panel talks to the backend
// through TanStack Query. Stub them all so this suite exercises only the modal
// shell's dismissal behaviour, not any panel's data-fetching. Factories are
// hoisted above imports, so each must be self-contained (no shared helper).
vi.mock('./GeneralSettings', () => ({ GeneralSettings: () => <div>GeneralSettings</div> }));
vi.mock('./MCPSettings', () => ({ MCPSettings: () => <div>MCPSettings</div> }));
vi.mock('./ImportPanel', () => ({ ImportPanel: () => <div>ImportPanel</div> }));
vi.mock('./SkillsPanel', () => ({ SkillsPanel: () => <div>SkillsPanel</div> }));
vi.mock('./CostsPanel', () => ({ CostsPanel: () => <div>CostsPanel</div> }));
vi.mock('./HooksPanel', () => ({ HooksPanel: () => <div>HooksPanel</div> }));
vi.mock('./CronPanel', () => ({ CronPanel: () => <div>CronPanel</div> }));
vi.mock('./PluginsPanel', () => ({ PluginsPanel: () => <div>PluginsPanel</div> }));
vi.mock('./ModelModePanel', () => ({ ModelModePanel: () => <div>ModelModePanel</div> }));
vi.mock('./ModelsPanel', () => ({ ModelsPanel: () => <div>ModelsPanel</div> }));
vi.mock('./FeatureFlagsPanel', () => ({ FeatureFlagsPanel: () => <div>FeatureFlagsPanel</div> }));

const isOpen = () => useUIStore.getState().settingsOpen;

beforeEach(() => {
  useUIStore.setState({ settingsOpen: true, settingsTab: 'general' });
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
    const contentPanel = screen.getByText('GeneralSettings');

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

  it('"skills", "plugins", "hooks" and "cron" each open their own panel', () => {
    for (const [id, panel, label] of [
      ['skills', 'SkillsPanel', 'Skills'],
      ['plugins', 'PluginsPanel', 'Plugins'],
      ['hooks', 'HooksPanel', 'Hooks'],
      ['cron', 'CronPanel', 'Scheduled tasks'],
    ] as const) {
      const { unmount } = openAt(id);
      expect(screen.getByText(panel)).toBeInTheDocument();
      expect(screen.getByRole('button', { name: label })).toHaveAttribute('aria-current', 'page');
      expect(screen.queryAllByRole('tab')).toHaveLength(0);
      unmount();
    }
  });

  it('keeps related tools together: what the assistant uses, then what runs on its own', () => {
    openAt('general');
    const group = screen.getByRole('group', { name: 'Tools and automation' });
    const items = [...group.querySelectorAll('.settings-rail-item')].map((b) => b.textContent?.trim());
    expect(items).toEqual(['Skills', 'Plugins', 'MCP servers', 'Code tools', 'Hooks', 'Scheduled tasks', 'Import project']);
  });

  it('"costs" opens Costs as its own panel with no sub-tab strip', () => {
    openAt('costs');
    expect(screen.getByText('CostsPanel')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Costs' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
  });

  it('"feature-flags" opens Feature flags as its own panel with no sub-tab strip', () => {
    openAt('feature-flags');
    expect(screen.getByText('FeatureFlagsPanel')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Feature flags' })).toHaveAttribute('aria-current', 'page');
    expect(screen.queryAllByRole('tab')).toHaveLength(0);
  });

  it('"general" opens the General panel first in the rail', () => {
    openAt('general');
    expect(screen.getByText('GeneralSettings')).toBeInTheDocument();
    const general = screen.getByRole('button', { name: 'General' });
    expect(general).toHaveAttribute('aria-current', 'page');
    expect(screen.getAllByRole('button').find((b) => b.classList.contains('settings-rail-item'))).toBe(general);
  });

  it('has no Permissions or Live preview destination', () => {
    openAt('general');
    expect(screen.queryByRole('button', { name: /Permissions/i })).toBeNull();
    expect(screen.queryByRole('button', { name: /Live preview/i })).toBeNull();
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

  it('an unknown tab id falls back to General', () => {
    openAt('no-such-tab');
    expect(screen.getByText('GeneralSettings')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'General' })).toHaveAttribute('aria-current', 'page');
  });

  it('clicking a rail item then a sub-tab swaps the visible panel', () => {
    openAt('general');
    // Jump to Models via the rail, then to its Discover sub-tab.
    fireEvent.click(screen.getByRole('button', { name: 'Models' }));
    expect(screen.getByRole('tab', { name: 'Chat' })).toHaveAttribute('aria-selected', 'true');
    fireEvent.click(screen.getByRole('tab', { name: 'Discover' }));
    expect(screen.getByRole('tab', { name: 'Discover' })).toHaveAttribute('aria-selected', 'true');
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
