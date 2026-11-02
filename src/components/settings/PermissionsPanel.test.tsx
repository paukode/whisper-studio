import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';

/**
 * Every Save in the panel writes its state back to the server, so a failed
 * load must never leave the defaults on screen with live Save buttons: that
 * would overwrite the saved mode, rules and network tier.
 */
const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));
vi.mock('@/api/client', () => api);

import { PermissionsPanel } from './PermissionsPanel';

afterEach(() => vi.clearAllMocks());

describe('PermissionsPanel load', () => {
  it('shows the load error and offers no Save when the permissions cannot be read', async () => {
    api.get.mockRejectedValue(new Error('database is locked'));
    render(<PermissionsPanel />);
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Could not load the permissions: database is locked',
    );
    expect(screen.queryByRole('button', { name: /Save/ })).toBeNull();
  });

  it('says why the approved folders are missing instead of "none yet"', async () => {
    api.get.mockImplementation(async (url: string) => {
      if (url === '/api/permissions') return { mode: 'default', rules: [], categories: [], category_modes: {} };
      throw new Error('permission denied');
    });
    render(<PermissionsPanel />);
    expect((await screen.findByRole('alert')).textContent).toContain(
      'Could not load the approved folders: permission denied',
    );
    expect(screen.queryByText(/No folders approved yet/)).toBeNull();
    expect(screen.getByRole('button', { name: 'Save Mode' })).toBeTruthy();
  });
});
