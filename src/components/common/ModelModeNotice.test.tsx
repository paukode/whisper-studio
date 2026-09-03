import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';

// markModeNoticeSeen persists via put() from the api client.
const { putMock, getMock } = vi.hoisted(() => ({ putMock: vi.fn(), getMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, put: putMock }));

import { ModelModeNotice } from './ModelModeNotice';
import { useSettingsStore } from '@/stores/settingsStore';

/** The notice is config-gated: it only appears once the loaded config says
 *  it is unseen (the pre-load default is seen). */
function markUnseen(): void {
  useSettingsStore.setState((s) => ({ config: { ...s.config, modeNoticeSeen: false } }));
}

describe('ModelModeNotice', () => {
  beforeEach(() => {
    localStorage.clear();
    putMock.mockReset().mockResolvedValue({});
    useSettingsStore.setState((s) => ({ config: { ...s.config, modeNoticeSeen: true } }));
  });

  it('stays hidden until the loaded config reports it unseen (no pre-load flash)', () => {
    render(<ModelModeNotice />);
    expect(screen.queryByText(/starting in Local mode/i)).toBeNull();
  });

  it('shows once config says unseen and explains the three modes', () => {
    markUnseen();
    render(<ModelModeNotice />);
    expect(screen.getByText(/starting in Local mode/i)).toBeInTheDocument();
    expect(screen.getByText('Local')).toBeInTheDocument();
    expect(screen.getByText('Hybrid')).toBeInTheDocument();
    expect(screen.getByText('Cloud')).toBeInTheDocument();
  });

  it('dismiss persists to config (survives origin changes) and across mounts', () => {
    markUnseen();
    const { unmount } = render(<ModelModeNotice />);
    fireEvent.click(screen.getByText('Got it'));
    expect(putMock).toHaveBeenCalledWith('/api/config', { mode_notice_seen: true });
    expect(useSettingsStore.getState().config.modeNoticeSeen).toBe(true);
    unmount();
    render(<ModelModeNotice />);
    expect(screen.queryByText(/starting in Local mode/i)).toBeNull();
  });

  it('the legacy localStorage flag still suppresses it', () => {
    markUnseen();
    localStorage.setItem('whisper_mode_notice_v1', '1');
    render(<ModelModeNotice />);
    expect(screen.queryByText(/starting in Local mode/i)).toBeNull();
  });
});
