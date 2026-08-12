/**
 * The composer readout is the app's only tokens/cost/context surface, so what
 * it shows has to be the SESSION's totals (not one turn's) and its "in" side
 * has to be real prompt tokens — the bug it replaced read "4 in / 4,012 out"
 * on a warm prompt cache.
 */
import { render } from '@testing-library/react';
import { describe, it, expect, beforeEach } from 'vitest';

import { TokenCounter, formatTokenCount } from './TokenCounter';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';

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
});
