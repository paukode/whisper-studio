import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CostsPanel } from './CostsPanel';
import { usageFor } from '@/test/fixtures/costsUsage';
import type { UsageTotals } from '@/types/costs';

// The panel reads/writes budget settings via the api client. Route GET by URL
// so /api/config seeds the form and the usage endpoint answers in the
// contract shape (src/test/fixtures/costs-usage.json).
const CONFIG = {
  max_session_cost_usd: 3.5,
  max_daily_cost_usd: 12,
  model_fallback_enabled: true,
  round_limit: 90,
  time_limit_minutes: 20,
};

// Today's UTC day in these tests, and the report the daily-cap hint reads.
const TODAY_URL = '/api/costs/usage?from=2026-09-23&to=2026-09-23&granularity=day&split=model';
// Totals a test lays over today's report (read when the request is made).
let todayTotals: Partial<UsageTotals> | null = null;

vi.mock('@/api/client', () => ({
  get: vi.fn((url: string) => {
    if (url === '/api/config') return Promise.resolve(CONFIG);
    if (url === TODAY_URL && todayTotals) {
      const report = usageFor(url);
      return Promise.resolve({ ...report, totals: { ...report.totals, ...todayTotals } });
    }
    if (url.startsWith('/api/costs/usage')) return Promise.resolve(usageFor(url));
    return Promise.resolve({});
  }),
  put: vi.fn(() => Promise.resolve({ updated: true })),
  post: vi.fn(() => Promise.resolve({})),
  del: vi.fn(() => Promise.resolve({})),
}));

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CostsPanel />
    </QueryClientProvider>,
  );
}

describe('CostsPanel: budget save wiring', () => {
  beforeEach(() => {
    vi.clearAllMocks();
    vi.useFakeTimers({ toFake: ['Date'] });
    // Late in the UTC day (already the 24th in Warsaw): the cap counts the UTC day.
    vi.setSystemTime(new Date('2026-09-23T23:30:00Z'));
  });
  afterEach(() => {
    vi.useRealTimers();
    todayTotals = null;
  });

  it('seeds the budget fields from GET /api/config on mount', async () => {
    const { container } = renderPanel();

    const session = () => container.querySelector<HTMLInputElement>('#budgetMaxSession')!;
    const daily = () => container.querySelector<HTMLInputElement>('#budgetMaxDaily')!;
    const fallback = () => container.querySelector<HTMLInputElement>('#budgetModelFallback')!;

    await waitFor(() => expect(session().value).toBe('3.5'));
    expect(daily().value).toBe('12');
    expect(fallback().checked).toBe(true);
    expect(container.querySelector<HTMLInputElement>('#budgetRoundLimit')!.value).toBe('90');
    expect(container.querySelector<HTMLInputElement>('#budgetTimeLimit')!.value).toBe('20');
  });

  it('saves the round and time limits with the caps', async () => {
    const { put } = await import('@/api/client');
    const { container } = renderPanel();
    const rounds = () => container.querySelector<HTMLInputElement>('#budgetRoundLimit')!;
    const minutes = () => container.querySelector<HTMLInputElement>('#budgetTimeLimit')!;
    await waitFor(() => expect(rounds().value).toBe('90'));

    fireEvent.change(rounds(), { target: { value: '150' } });
    fireEvent.change(minutes(), { target: { value: '30' } });
    fireEvent.click(screen.getByRole('button', { name: /save budget/i }));

    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/config', expect.anything()));
    const body = (put as ReturnType<typeof vi.fn>).mock.calls.find(
      (c) => c[0] === '/api/config',
    )![1] as Record<string, unknown>;
    expect(body).toMatchObject({ round_limit: 150, time_limit_minutes: 30 });
  });

  it.each(['', '0', '2.5'])('refuses to save a round limit of %j', async (value) => {
    const { put } = await import('@/api/client');
    const { container } = renderPanel();
    const rounds = () => container.querySelector<HTMLInputElement>('#budgetRoundLimit')!;
    await waitFor(() => expect(rounds().value).toBe('90'));

    fireEvent.change(rounds(), { target: { value } });
    fireEvent.click(screen.getByRole('button', { name: /save budget/i }));

    await waitFor(() =>
      expect(
        screen.getByText('Round and time limits must be whole numbers, 1 or more'),
      ).toBeTruthy(),
    );
    expect(put).not.toHaveBeenCalled();
  });

  it('PUTs the real backend config keys (not the old names) on save', async () => {
    const { put } = await import('@/api/client');
    const { container } = renderPanel();

    // Wait for the seed so we know config loaded, then override the inputs.
    const session = () => container.querySelector<HTMLInputElement>('#budgetMaxSession')!;
    const daily = () => container.querySelector<HTMLInputElement>('#budgetMaxDaily')!;
    await waitFor(() => expect(session().value).toBe('3.5'));

    fireEvent.change(session(), { target: { value: '5' } });
    fireEvent.change(daily(), { target: { value: '20' } });

    fireEvent.click(screen.getByRole('button', { name: /save budget/i }));

    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/config', expect.anything()));

    const body = (put as ReturnType<typeof vi.fn>).mock.calls.find(
      (c) => c[0] === '/api/config',
    )![1] as Record<string, unknown>;

    // Correct keys, correct types.
    expect(body).toMatchObject({
      max_session_cost_usd: 5,
      max_daily_cost_usd: 20,
      model_fallback_enabled: true,
    });

    // The old names that update_config silently dropped must be gone.
    expect(body).not.toHaveProperty('max_session_cost');
    expect(body).not.toHaveProperty('max_daily_cost');
    expect(body).not.toHaveProperty('model_fallback');
  });

  it('clearing a field sends 0, not an omitted key, so the old limit cannot survive the save', async () => {
    const { put } = await import('@/api/client');
    const { container } = renderPanel();

    const daily = () => container.querySelector<HTMLInputElement>('#budgetMaxDaily')!;
    await waitFor(() => expect(daily().value).toBe('12'));

    fireEvent.change(daily(), { target: { value: '' } });
    fireEvent.click(screen.getByRole('button', { name: /save budget/i }));

    await waitFor(() => expect(put).toHaveBeenCalledWith('/api/config', expect.anything()));

    const body = (put as ReturnType<typeof vi.fn>).mock.calls.find(
      (c) => c[0] === '/api/config',
    )![1] as Record<string, unknown>;

    expect(body).toHaveProperty('max_daily_cost_usd', 0);
  });

  it('shows "Saved!" after a successful save', async () => {
    const { container } = renderPanel();
    await waitFor(() =>
      expect(container.querySelector<HTMLInputElement>('#budgetMaxSession')!.value).toBe('3.5'),
    );

    fireEvent.click(screen.getByRole('button', { name: /save budget/i }));
    await waitFor(() => expect(screen.getByText('Saved!')).toBeTruthy());
  });

  it("shows today's UTC-day spend under the daily cap, from the usage report", async () => {
    const { get } = await import('@/api/client');
    renderPanel();
    await waitFor(() =>
      // The fixture's spend on 2026-09-23 alone, not the 30-day report's $76.55.
      expect(screen.getByText('Today so far (UTC day): $27.55')).toBeInTheDocument(),
    );
    expect(get).toHaveBeenCalledWith(TODAY_URL);
    expect(screen.getByLabelText('Max Daily Cost (USD, UTC day)')).toBeInTheDocument();
  });

  it("marks today's spend as estimated when it rests on estimated token counts", async () => {
    // As on this Mac on 2026-09-21: 13 of 346 calls that UTC day estimated.
    todayTotals = { cost_usd: 133.308186, calls: 346, estimated_calls: 13 };
    const { container } = renderPanel();
    const hint = () => container.querySelector('#budgetTodaySoFar')!;
    await waitFor(() => expect(hint().textContent).toContain('$133.31'));
    expect(hint()).toHaveClass('usage-est');
    expect(hint().textContent).toContain('includes 13 of 346 calls with estimated token counts');
  });

  it('shows a fully reported day as a plain figure', async () => {
    todayTotals = { cost_usd: 4.2, calls: 12, estimated_calls: 0 };
    const { container } = renderPanel();
    const hint = () => container.querySelector('#budgetTodaySoFar')!;
    await waitFor(() => expect(hint().textContent).toBe('Today so far (UTC day): $4.20'));
    expect(hint()).not.toHaveClass('usage-est');
  });

  it('the hint moves to the new UTC day at 00:00 UTC while the tab stays open', async () => {
    const { get } = await import('@/api/client');
    renderPanel();
    await waitFor(() =>
      expect(screen.getByText('Today so far (UTC day): $27.55')).toBeInTheDocument(),
    );

    // Past midnight UTC (the timer is real, so the focus on return catches up).
    vi.setSystemTime(new Date('2026-09-24T00:00:05Z'));
    fireEvent.focus(window);
    await waitFor(() =>
      expect(screen.getByText('Today so far (UTC day): $0.00')).toBeInTheDocument(),
    );
    expect(get).toHaveBeenCalledWith(
      '/api/costs/usage?from=2026-09-24&to=2026-09-24&granularity=day&split=model',
    );
    // The default range moved with it.
    expect(get).toHaveBeenCalledWith(
      '/api/costs/usage?from=2026-08-26&to=2026-09-24&granularity=day&split=model',
    );
  });

  it('offers no way to delete spend: the reset control and its endpoint are gone', async () => {
    const { post } = await import('@/api/client');
    renderPanel();
    await screen.findByRole('button', { name: /save budget/i });
    expect(screen.queryByRole('button', { name: /reset/i })).toBeNull();
    expect(post).not.toHaveBeenCalled();
  });
});
