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
