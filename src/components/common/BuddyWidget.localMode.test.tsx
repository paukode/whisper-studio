import { describe, it, expect, vi, beforeEach } from 'vitest';
import { render, screen, fireEvent, waitFor } from '@testing-library/react';

const { getMock, postMock, putMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  postMock: vi.fn(),
  putMock: vi.fn(),
}));
vi.mock('@/api/client', () => ({ get: getMock, post: postMock, put: putMock }));

import { BuddyWidget } from './BuddyWidget';
import { useSettingsStore } from '@/stores/settingsStore';

const BUDDY = {
  hatched: true,
  name: 'Pip',
  bones: { rarity: 'common', species: 'cat', eye: 'dot', hat: 'none', shiny: false, stats: {} },
  color: '#888',
  stars: '*',
};

function setMode(mode: 'local' | 'hybrid' | 'cloud'): void {
  useSettingsStore.setState((s) => ({ config: { ...s.config, modelMode: mode } }));
}

function serve(fact: Record<string, string>): void {
  getMock.mockReset().mockImplementation(async (url: string) => {
    if (url === '/api/feature-flags') return { companion: { enabled: true } };
    if (url === '/api/buddy') return BUDDY;
    if (url === '/api/buddy/fact') return fact;
    throw new Error(`unexpected ${url}`);
  });
}

const factCalls = () => getMock.mock.calls.filter(([url]) => url === '/api/buddy/fact').length;

/** Fresh AI facts come from a cloud model: in Local mode the widget never asks
 *  for one, serves its curated pack, and says why the toggle is off. */
describe('BuddyWidget fresh facts and Local mode', () => {
  beforeEach(() => {
    localStorage.clear();
    localStorage.setItem('buddy_ai_facts', '1'); // the user opted in earlier
    serve({ fact: 'A fresh cloud fact.' });
  });

  it('turns the toggle off with the reason in Local mode', async () => {
    setMode('local');
    render(<BuddyWidget />);
    const creature = await screen.findByTitle(/click to pet/i);
    fireEvent.mouseEnter(creature.closest('.buddy-widget')!);
    const toggle = screen.getByRole('checkbox');
    expect(toggle).toBeDisabled();
    expect(toggle).not.toBeChecked();
    expect(toggle.closest('label')?.getAttribute('title')).toMatch(/Local mode/);
    expect(screen.getByText('(AI, off in Local mode)')).toBeInTheDocument();
  });

  it('serves a curated fact without asking the server in Local mode', async () => {
    setMode('local');
    render(<BuddyWidget />);
    fireEvent.click(await screen.findByTitle(/click to pet/i));
    await waitFor(() => expect(screen.getByText(/did you know/i)).toBeInTheDocument());
    expect(factCalls()).toBe(0);
    expect(screen.queryByText('A fresh cloud fact.')).toBeNull();
  });

  it('fetches the fresh fact outside Local mode', async () => {
    setMode('hybrid');
    render(<BuddyWidget />);
    const creature = await screen.findByTitle(/click to pet/i);
    fireEvent.mouseEnter(creature.closest('.buddy-widget')!);
    expect(screen.getByRole('checkbox')).toBeEnabled();
    fireEvent.click(creature);
    await waitFor(() => expect(screen.getByText('A fresh cloud fact.')).toBeInTheDocument());
  });

  it('keeps a server refusal instead of showing it as a fact', async () => {
    setMode('hybrid'); // this window has not seen a switch to Local mode yet
    serve({ reason: 'Fresh facts (AI) uses Amazon Bedrock, and Local mode keeps everything on this Mac.' });
    render(<BuddyWidget />);
    const creature = await screen.findByTitle(/click to pet/i);
    fireEvent.click(creature);
    await waitFor(() => expect(screen.getByText(/did you know/i)).toBeInTheDocument());
    expect(screen.queryByText(/uses Amazon Bedrock/)).toBeNull();
    fireEvent.click(creature); // a fetch would start synchronously here
    expect(factCalls()).toBe(1); // not asked again
  });
});
