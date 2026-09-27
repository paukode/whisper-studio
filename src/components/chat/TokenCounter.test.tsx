/**
 * The composer readout is the app's only tokens/cost/context surface, so what
 * it shows has to be the SESSION's totals (not one turn's) and its "in" side
 * has to be real prompt tokens — the bug it replaced read "4 in / 4,012 out"
 * on a warm prompt cache.
 */
import { render } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('@/api/client', () => api);

import { TokenCounter, formatTokenCount } from './TokenCounter';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import { refreshSessionUsage } from '@/stores/sessionUsage';

// As the server words it (server/costs/usage.py GPT_MIXED_NOTE).
const GPT_MIXED_NOTE =
  'Includes GPT on Bedrock, estimated at list rates. AWS billed GPT on Bedrock at $0 for this ' +
  'account as of 2026-09-23.';

const SID = 'tc-test-session';

function primeActiveSession() {
  const store = getChatStore(SID);
  store.setState({
    inputTokens: 0,
    outputTokens: 0,
    estimatedCost: 0,
    sessionInputTokens: 0,
    sessionOutputTokens: 0,
    sessionCost: 0,
    sessionCostNote: '',
    sessionEstimatedRounds: 0,
    contextUsed: 0,
    contextMax: 0,
  });
  useSessionStore.setState({ currentSessionId: SID });
  return store;
}

describe('formatTokenCount', () => {
  it('keeps sub-1K counts exact and compacts the rest', () => {
    expect(formatTokenCount(0)).toBe('0');
    expect(formatTokenCount(940)).toBe('940');
    expect(formatTokenCount(4012)).toBe('4.0K');
    expect(formatTokenCount(118_400)).toBe('118K');
    expect(formatTokenCount(2_450_000)).toBe('2.5M');
  });
});

describe('TokenCounter', () => {
  beforeEach(() => {
    primeActiveSession();
  });

  it('renders nothing before any usage frame', () => {
    const { container } = render(<TokenCounter />);
    expect(container.querySelector('.token-counter')).toBeNull();
  });

  it('shows session totals, not the current turn', () => {
    const store = primeActiveSession();
    // Two turns: the store resets the per-turn fields between them, exactly as
    // setStreaming does at the head of a stream.
    store.getState().setUsage(60_000, 2_000, 0.3, 60_000, 200_000);
    store.setState({ inputTokens: 0, outputTokens: 0, estimatedCost: 0 });
    store.getState().setUsage(58_400, 2_012, 0.1679, 58_400, 200_000);

    const { container } = render(<TokenCounter />);
    const text = container.textContent ?? '';
    expect(text).toContain('118K in');
    expect(text).toContain('4.0K out');
    expect(text).toContain('$0.4679');
  });

  it('renders the context meter from the last turn', () => {
    const store = primeActiveSession();
    store.getState().setUsage(1000, 200, 0.05, 100_000, 200_000);
    const { container } = render(<TokenCounter />);
    expect(container.textContent).toContain('50% ctx');
    const fill = container.querySelector('.tc-ctx-fill') as HTMLElement;
    expect(fill.style.width).toBe('50%');
  });

  it('marks the context meter hot at >=80%', () => {
    const store = primeActiveSession();
    store.getState().setUsage(1000, 200, 0.05, 170_000, 200_000);
    const { container } = render(<TokenCounter />);
    expect(container.querySelector('.tc-ctx-fill.hot')).toBeTruthy();
  });

  it('omits the meter until the window is known', () => {
    const store = primeActiveSession();
    store.getState().setUsage(1000, 200, 0.05);
    const { container } = render(<TokenCounter />);
    expect(container.querySelector('.tc-ctx-track')).toBeNull();
    expect(container.textContent).toContain('1.0K in');
  });

  it('says on hover that the dollar figure is an estimate at list rates', () => {
    // The app never sees the AWS bill, so the cost must not read as billed.
    const store = primeActiveSession();
    store.getState().setUsage(1000, 200, 0.05);
    const { container } = render(<TokenCounter />);
    const title = container.querySelector('.token-counter')?.getAttribute('title') ?? '';
    expect(title).toContain('$0.0500 estimated at list rates');
  });

  it("marks a GPT session's cost with the dated list-rate note, as the Costs tab row does", async () => {
    // The real session behind the finding: 257 GPT-6 Astra rounds, 7 Opus 5.
    primeActiveSession();
    api.get.mockResolvedValue({
      prompt_tokens: 47_978_890,
      output_tokens: 306_118,
      cost_usd: 116.2235,
      rounds: 264,
      gpt_rounds: 257,
      estimated_rounds: 0,
      note: GPT_MIXED_NOTE,
    });
    await refreshSessionUsage(SID);
    const { container } = render(<TokenCounter />);
    expect(api.get).toHaveBeenCalledWith(`/api/costs/session/${SID}`);
    const note = container.querySelector('#tokenCounterNote');
    expect(note).toHaveTextContent('GPT at list rates');
    expect(note?.getAttribute('aria-label')).toBe(GPT_MIXED_NOTE);
    const title = container.querySelector('.token-counter')?.getAttribute('title') ?? '';
    expect(title).toContain('AWS billed GPT on Bedrock at $0 for this account as of 2026-09-23');
    expect(container.querySelector('.tc-cost')).not.toHaveClass('usage-est');
  });

  it('shows a Claude-only session without the GPT note', async () => {
    primeActiveSession();
    api.get.mockResolvedValue({ prompt_tokens: 1000, output_tokens: 10, cost_usd: 0.01, note: '' });
    await refreshSessionUsage(SID);
    const { container } = render(<TokenCounter />);
    expect(container.querySelector('#tokenCounterNote')).toBeNull();
  });

  it('reads as estimated when some rounds had no token count from the provider', async () => {
    primeActiveSession();
    api.get.mockResolvedValue({
      prompt_tokens: 1000,
      output_tokens: 10,
      cost_usd: 0.01,
      note: '',
      estimated_rounds: 3,
    });
    await refreshSessionUsage(SID);
    const { container } = render(<TokenCounter />);
    expect(container.querySelector('.tc-cost')).toHaveClass('usage-est');
    const title = container.querySelector('.token-counter')?.getAttribute('title') ?? '';
    expect(title).toContain('3 of its rounds had no token count from the provider');
  });
});
