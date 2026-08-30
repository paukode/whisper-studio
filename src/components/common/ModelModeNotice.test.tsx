import { describe, it, expect, beforeEach } from 'vitest';
import { render, screen, fireEvent } from '@testing-library/react';
import { ModelModeNotice } from './ModelModeNotice';

describe('ModelModeNotice', () => {
  beforeEach(() => localStorage.clear());

  it('shows once and explains the three modes', () => {
    render(<ModelModeNotice />);
    expect(screen.getByText(/starting in Local mode/i)).toBeInTheDocument();
    expect(screen.getByText('Local')).toBeInTheDocument();
    expect(screen.getByText('Hybrid')).toBeInTheDocument();
    expect(screen.getByText('Cloud')).toBeInTheDocument();
  });

  it('dismiss persists across mounts', () => {
    const { unmount } = render(<ModelModeNotice />);
    fireEvent.click(screen.getByText('Got it'));
    unmount();
    render(<ModelModeNotice />);
    expect(screen.queryByText(/starting in Local mode/i)).toBeNull();
  });

  it('never renders once the flag is set', () => {
    localStorage.setItem('whisper_mode_notice_v1', '1');
    render(<ModelModeNotice />);
    expect(screen.queryByText(/starting in Local mode/i)).toBeNull();
  });
});
