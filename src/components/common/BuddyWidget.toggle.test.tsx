import { describe, it, expect, vi, beforeEach } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';

const { getMock, postMock } = vi.hoisted(() => ({ getMock: vi.fn(), postMock: vi.fn() }));
vi.mock('@/api/client', () => ({ get: getMock, post: postMock }));

import { BuddyWidget } from './BuddyWidget';

const BUDDY = {
  hatched: true,
  name: 'Pip',
  personality: '',
  bones: { rarity: 'common', species: 'blob', eye: 'dot', hat: 'none', shiny: false, stats: {} },
  color: '#888',
  stars: '*',
};

/** The smiley is the companion's one control: hidden until the first click,
 *  then it shows and hides the creature. No feature flag is read. */
describe('BuddyWidget smiley', () => {
  beforeEach(() => {
    localStorage.clear();
    getMock.mockReset().mockResolvedValue(BUDDY);
    postMock.mockReset();
  });

  it('starts hidden and loads nothing until the smiley is clicked', async () => {
    const { container } = render(<BuddyWidget />);
    expect(container.querySelector('.buddy-widget')).toBeNull();
    expect(getMock).not.toHaveBeenCalled();

    fireEvent.click(screen.getByRole('button', { name: 'Show companion' }));
    await waitFor(() => expect(container.querySelector('.buddy-widget')).not.toBeNull());
    expect(getMock).toHaveBeenCalledWith('/api/buddy');
    expect(getMock).not.toHaveBeenCalledWith('/api/feature-flags');
    expect(JSON.parse(localStorage.getItem('buddy_state') ?? '{}').hidden).toBe(false);

    fireEvent.click(screen.getByRole('button', { name: 'Hide companion' }));
    expect(container.querySelector('.buddy-widget')).toBeNull();
    expect(JSON.parse(localStorage.getItem('buddy_state') ?? '{}').hidden).toBe(true);
  });
});
