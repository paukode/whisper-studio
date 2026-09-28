import { beforeEach, describe, expect, it } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { LspStatusIndicator } from './LspStatusIndicator';
import { useUIStore } from '@/stores/uiStore';

const REASON = 'typescript-language-server is not installed. Install it with npm.';

beforeEach(() => {
  useUIStore.setState({ settingsOpen: false, settingsTab: 'apikeys' });
});

describe('LspStatusIndicator', () => {
  it('names the reason a language server is not running', () => {
    render(<LspStatusIndicator status="error" language="typescript" failure={REASON} />);
    const pill = screen.getByRole('button', { name: /typescript language server: unavailable/ });
    expect(pill).toHaveAccessibleName(expect.stringContaining(REASON));
    expect(pill.getAttribute('title')).toContain(REASON);
  });

  it('opens Settings > Code tools from a failed server', () => {
    render(<LspStatusIndicator status="error" language="python" failure={REASON} />);
    fireEvent.click(screen.getByRole('button'));
    expect(useUIStore.getState().settingsOpen).toBe(true);
    expect(useUIStore.getState().settingsTab).toBe('code-tools');
  });

  it('stays a plain status while the server runs', () => {
    render(<LspStatusIndicator status="connected" language="python" failure={null} />);
    expect(screen.getByRole('status')).toHaveAccessibleName('python language server: connected');
    expect(screen.queryByRole('button')).toBeNull();
  });
});
