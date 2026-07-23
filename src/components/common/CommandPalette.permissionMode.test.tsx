import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// The palette's "Set permission mode: X" entries used to just open the Settings
// tab. They must APPLY the mode via the same write path as ChatInput's Mode
// dropdown (handleSelectMode): flip the Plan flag, update the config's
// permissionMode, then PUT /api/permissions/mode.
vi.mock('@/api/client', () => ({ put: vi.fn(() => Promise.resolve({})) }));
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ themes: [], setTheme: vi.fn(), resolvedTheme: 'dark', themeKey: 'dark' }),
}));
vi.mock('@/hooks/useDismiss', () => ({ useDismiss: () => {} }));
vi.mock('@/components/chat/dataRetentionConsent', () => ({ requestModelChange: vi.fn() }));

import { CommandPalette } from './CommandPalette';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { put } from '@/api/client';

describe('CommandPalette — permission mode entries apply the mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useUIStore.setState({ commandPaletteOpen: true });
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('Set permission mode: Plan flips the plan flag on and PUTs mode "plan"', () => {
    // Spy before render so the useMemo captures the spied action.
    const setPlanMode = vi.spyOn(useSettingsStore.getState(), 'setPlanMode');
    const updateConfig = vi.spyOn(useSettingsStore.getState(), 'updateConfig');

    render(<CommandPalette />);
    fireEvent.click(screen.getByText('Set permission mode: Plan'));

    expect(setPlanMode).toHaveBeenCalledWith(true);
    expect(updateConfig).toHaveBeenCalledWith({ permissionMode: 'plan' });
    expect(put).toHaveBeenCalledWith('/api/permissions/mode', { mode: 'plan' });
  });

  it('a non-plan mode uses its own key and leaves the plan flag off', () => {
    const setPlanMode = vi.spyOn(useSettingsStore.getState(), 'setPlanMode');
    const updateConfig = vi.spyOn(useSettingsStore.getState(), 'updateConfig');

    render(<CommandPalette />);
    fireEvent.click(screen.getByText('Set permission mode: Accept edits'));

    expect(setPlanMode).toHaveBeenCalledWith(false);
    expect(updateConfig).toHaveBeenCalledWith({ permissionMode: 'acceptEdits' });
    expect(put).toHaveBeenCalledWith('/api/permissions/mode', { mode: 'acceptEdits' });
  });
});
