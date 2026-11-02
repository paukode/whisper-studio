import { describe, expect, it, vi, beforeEach, afterEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

// The palette's "Set permission mode: X" entries apply the mode through the
// same write as ChatInput's Mode dropdown: PUT /api/permissions/mode first,
// then the store. A failed write keeps the old mode and says why.
vi.mock('@/api/client', () => ({ put: vi.fn(() => Promise.resolve({ mode: 'plan' })) }));
vi.mock('@/providers/ThemeProvider', () => ({
  useTheme: () => ({ themes: [], setTheme: vi.fn(), resolvedTheme: 'dark', themeKey: 'dark' }),
}));
vi.mock('@/hooks/useDismiss', () => ({ useDismiss: () => {} }));
vi.mock('@/components/chat/dataRetentionConsent', () => ({ requestModelChange: vi.fn() }));

import { CommandPalette } from './CommandPalette';
import { useUIStore } from '@/stores/uiStore';
import { useSettingsStore } from '@/stores/settingsStore';
import { put } from '@/api/client';

describe('CommandPalette: permission mode entries apply the mode', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    useUIStore.setState({ commandPaletteOpen: true, toasts: [] });
    useSettingsStore.setState((s) => ({ config: { ...s.config, permissionMode: 'default' } }));
  });
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('Set permission mode: Plan PUTs mode "plan" and then shows it', async () => {
    render(<CommandPalette />);
    fireEvent.click(screen.getByText('Set permission mode: Plan'));

    expect(put).toHaveBeenCalledWith('/api/permissions/mode', { mode: 'plan' });
    await waitFor(() => expect(useSettingsStore.getState().config.permissionMode).toBe('plan'));
  });

  it('a non-plan mode uses its own key', async () => {
    render(<CommandPalette />);
    fireEvent.click(screen.getByText('Set permission mode: Accept edits'));

    expect(put).toHaveBeenCalledWith('/api/permissions/mode', { mode: 'acceptEdits' });
    await waitFor(() => expect(useSettingsStore.getState().config.permissionMode).toBe('acceptEdits'));
  });

  it('a refused write keeps the old mode and shows the reason', async () => {
    vi.mocked(put).mockRejectedValueOnce(new Error('mode must be one of: ...'));
    render(<CommandPalette />);
    fireEvent.click(screen.getByText('Set permission mode: Bypass'));

    await waitFor(() =>
      expect(useUIStore.getState().toasts.map((t) => t.message)).toContain(
        'Could not switch the permission mode: mode must be one of: ...',
      ),
    );
    expect(useSettingsStore.getState().config.permissionMode).toBe('default');
  });
});
