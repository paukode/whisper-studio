/**
 * The Costs tab renders one GET /api/costs/usage report (the contract shape
 * in src/test/fixtures/costs-usage.json). The old tests mocked a `{daily: []}`
 * shape the server never sent, which is how an always-empty chart passed.
 * These pin what the tab asks for, how it draws the answer, and that a
 * mismatched answer is shown as an error rather than as an empty view.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CostsPanel } from './CostsPanel';
import { GPT_NOTE, LOCAL_MODEL as LOCAL, usageFor } from '@/test/fixtures/costsUsage';
import { STORAGE_KEYS } from '@/utils/storageKeys';

const { getMock, downloadUrlMock } = vi.hoisted(() => ({
  getMock: vi.fn(),
  downloadUrlMock: vi.fn(),
}));

vi.mock('@/api/client', () => ({
  get: (url: string) => getMock(url),
  put: vi.fn(() => Promise.resolve({ updated: true })),
  post: vi.fn(() => Promise.resolve({})),
  del: vi.fn(() => Promise.resolve({})),
}));
vi.mock('@/utils/downloadFile', () => ({ downloadUrl: downloadUrlMock }));

function answerWithFixture() {
  getMock.mockImplementation((url: string) =>
    Promise.resolve(url.startsWith('/api/costs/usage') ? usageFor(url) : {}),
  );
}

const usageUrls = () =>
  getMock.mock.calls.map((c) => c[0] as string).filter((u) => u.startsWith('/api/costs/usage'));

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <CostsPanel />
    </QueryClientProvider>,
  );
}

function rememberView(view: Record<string, string>) {
  localStorage.setItem(
    STORAGE_KEYS.COSTS_VIEW,
    JSON.stringify({ range: 'last30', customFrom: '', customTo: '', granularity: 'day', split: 'model', ...view }),
  );
}

/** The fixture's own range by week: the buckets are exactly the JSON's. */
const FIXTURE_WEEKS = { range: 'custom', customFrom: '2026-09-01', customTo: '2026-09-23', granularity: 'week' };

const table = (container: HTMLElement) => container.querySelector<HTMLElement>('#costsBreakdown')!;

const rowKeys = (container: HTMLElement) =>
  [...container.querySelectorAll('#costsBreakdown tbody tr')].map((tr) => tr.getAttribute('data-key'));

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date'] });
  vi.setSystemTime(new Date('2026-09-23T12:00:00Z'));
  localStorage.clear();
  getMock.mockReset();
  downloadUrlMock.mockReset();
  answerWithFixture();
});

afterEach(() => {
  vi.useRealTimers();
});

describe('CostsPanel: what it asks for', () => {
  it('sends the selected range, granularity and split as UTC dates', async () => {
    renderPanel();
    // Default: the last 30 UTC days, by day, split by model.
    await waitFor(() =>
      expect(usageUrls()).toContain(
        '/api/costs/usage?from=2026-08-25&to=2026-09-23&granularity=day&split=model',
      ),
    );

    fireEvent.change(screen.getByLabelText('Range'), { target: { value: 'last7' } });
    await waitFor(() =>
      expect(usageUrls()).toContain(
        '/api/costs/usage?from=2026-09-17&to=2026-09-23&granularity=day&split=model',
      ),
    );

    fireEvent.click(within(screen.getByRole('group', { name: 'Granularity' })).getByRole('button', { name: 'Week' }));
    await waitFor(() =>
      expect(usageUrls()).toContain(
        '/api/costs/usage?from=2026-09-17&to=2026-09-23&granularity=week&split=model',
      ),
    );

    fireEvent.click(within(screen.getByRole('group', { name: 'Split by' })).getByRole('button', { name: 'Session' }));
    await waitFor(() =>
      expect(usageUrls()).toContain(
        '/api/costs/usage?from=2026-09-17&to=2026-09-23&granularity=week&split=session',
      ),
    );
  });

  it('remembers the last choices for the next time the tab opens', async () => {
    const first = renderPanel();
    fireEvent.click(within(screen.getByRole('group', { name: 'Granularity' })).getByRole('button', { name: 'Month' }));
    fireEvent.change(screen.getByLabelText('Range'), { target: { value: 'lastMonth' } });
    first.unmount();
    getMock.mockClear();

    renderPanel();
    expect(within(screen.getByRole('group', { name: 'Granularity' })).getByRole('button', { name: 'Month' })).toHaveAttribute(
      'aria-pressed',
      'true',
    );
    await waitFor(() =>
      expect(usageUrls()).toContain(
        '/api/costs/usage?from=2026-08-01&to=2026-08-31&granularity=month&split=model',
      ),
    );
  });

  it('a backwards custom range shows the reason and requests nothing for it', async () => {
    renderPanel();
    fireEvent.change(screen.getByLabelText('Range'), { target: { value: 'custom' } });
    // The pickers start on the range that was on screen.
    expect(screen.getByLabelText('From (UTC)')).toHaveValue('2026-08-25');
    expect(screen.getByLabelText('To (UTC)')).toHaveValue('2026-09-23');

    // To first, so every intermediate state is a valid range; only the last
    // edit makes it run backwards.
    fireEvent.change(screen.getByLabelText('To (UTC)'), { target: { value: '2026-09-10' } });
    fireEvent.change(screen.getByLabelText('From (UTC)'), { target: { value: '2026-09-20' } });

    expect(await screen.findByText('From must be on or before To.')).toBeInTheDocument();
    expect(usageUrls().some((u) => u.includes('from=2026-09-20'))).toBe(false);
    expect(screen.getByRole('button', { name: 'Export CSV' })).toBeDisabled();
  });

  it('exports exactly the selected range', async () => {
    renderPanel();
    fireEvent.change(screen.getByLabelText('Range'), { target: { value: 'thisMonth' } });
    fireEvent.click(screen.getByRole('button', { name: 'Export CSV' }));
    expect(downloadUrlMock).toHaveBeenCalledWith(
      '/api/costs/export?from=2026-09-01&to=2026-09-23&format=csv',
      'costs-2026-09-01-to-2026-09-23.csv',
    );
  });
});

describe('CostsPanel: how it draws the report', () => {
  it('draws every zero-filled bucket, the clipped weeks lighter', async () => {
    rememberView(FIXTURE_WEEKS);
    const { container } = renderPanel();
    await waitFor(() => expect(container.querySelectorAll('.usage-chart-col')).toHaveLength(4));

    const cols = [...container.querySelectorAll('.usage-chart-col')];
    expect(cols.map((c) => c.classList.contains('partial'))).toEqual([true, false, false, true]);
    // The bars run from the range's first day to its last.
    expect(cols[0]).toHaveAttribute('data-bucket', '2026-09-01');
    expect(cols[0]).toHaveAttribute('aria-label', 'Sep 1 to Sep 6 (partial): $20.00');
    // The quiet week is a bar of zero height, not a missing bar.
    const quiet = container.querySelector<HTMLElement>('[data-bucket="2026-09-14"] .usage-chart-bar')!;
    expect(quiet.style.height).toBe('0%');
    // A clipped week is named by the dates it covers.
    expect(cols[3]).toHaveAttribute('aria-label', 'Sep 21 to Sep 23 (partial): $27.55');
  });

  it('a range that starts mid-week starts its first bar on the range start, clipped', async () => {
    // Last 30 days on Wednesday 2026-09-23 starts on Tuesday 2026-08-25.
    rememberView({ granularity: 'week' });
    const { container } = renderPanel();
    await waitFor(() => expect(container.querySelectorAll('.usage-chart-col')).toHaveLength(5));
    const cols = [...container.querySelectorAll('.usage-chart-col')];
    expect(cols.map((c) => c.getAttribute('data-bucket'))).toEqual([
      '2026-08-25', '2026-08-31', '2026-09-07', '2026-09-14', '2026-09-21',
    ]);
    expect(cols.map((c) => c.classList.contains('partial'))).toEqual([true, false, false, false, true]);
    expect(cols[0]).toHaveAttribute('aria-label', 'Aug 25 to Aug 30 (partial): $0.00');
    expect(cols[1]).toHaveAttribute('aria-label', 'Aug 31 to Sep 6: $20.00');
  });

  it('stacks the top five models plus Other, and hover shows a bucket per model', async () => {
    rememberView(FIXTURE_WEEKS);
    const { container } = renderPanel();
    await waitFor(() => expect(container.querySelectorAll('.usage-legend li')).toHaveLength(6));
    expect([...container.querySelectorAll('.usage-legend li')].map((li) => li.textContent)).toEqual([
      'GPT-5.6 Sol',
      'GPT-6 Astra',
      'Opus 5',
      'Sonnet 5',
      'Haiku 4.5',
      'Other',
    ]);

    fireEvent.mouseEnter(container.querySelector('[data-bucket="2026-09-01"]')!);
    const tip = screen.getByRole('status');
    expect(tip).toHaveTextContent('Sep 1 to Sep 6 (partial): $20.00');
    expect(tip).toHaveTextContent('GPT-5.6 Sol $15.00 · 3,000,000 prompt · 20,000 output · 40 calls');
    expect(tip).toHaveTextContent('Opus 5 $5.00');

    // GPT-5.6 Sol has estimated counts in the range; Opus 5 has none.
    const entry = (name: string) =>
      within(tip).getByText(name, { selector: '.usage-chart-tip-name' }).closest('li')!;
    expect(entry('GPT-5.6 Sol')).toHaveClass('usage-est');
    expect(entry('GPT-5.6 Sol')).toHaveTextContent('may include estimated counts');
    expect(entry('Opus 5')).not.toHaveClass('usage-est');
    expect(entry('Opus 5')).not.toHaveTextContent('estimated');

    // "Other" holds the on-device model, whose counts were all estimated.
    fireEvent.mouseEnter(container.querySelector('[data-bucket="2026-09-07"]')!);
    expect(entry('Other')).toHaveClass('usage-est');
  });

  it('shows the range totals, marks estimated figures, and lists each pricing note once', async () => {
    const { container } = renderPanel();
    const cost = await waitFor(() => {
      const el = container.querySelector('#costsTileCost');
      expect(el).not.toBeNull();
      return el!;
    });
    expect(cost).toHaveTextContent('$76.55');
    expect(cost).toHaveTextContent('includes 8 of 193 calls with estimated token counts');
    expect(cost).toHaveClass('usage-est');
    expect(container.querySelector('#costsTileCache')).toHaveTextContent(
      'about $41.25 less than uncached, at list rates',
    );
    expect(container.querySelector('#costsRangeLine')).toHaveTextContent(
      'Aug 25, 2026 to Sep 23, 2026, UTC days',
    );
    // Two GPT rows carry the same dated note; the range lists it once.
    const notes = container.querySelector('#costsNotes')!;
    expect(within(notes as HTMLElement).getAllByText(GPT_NOTE)).toHaveLength(1);
  });

  it('points a noted row at its numbered pricing note and marks rows with estimated counts', async () => {
    const { container } = renderPanel();
    await waitFor(() => expect(rowKeys(container)).toHaveLength(7));
    const sol = container.querySelector<HTMLElement>('tr[data-key="gpt5.6-sol"]')!;
    // The row carries the note's number (the note on hover) and is described
    // by the note, which the panel lists once instead of under every row.
    const ref = within(sol).getByTitle(GPT_NOTE);
    const note = document.getElementById(sol.querySelector('th')!.getAttribute('aria-describedby')!)!;
    expect(note).toHaveTextContent(GPT_NOTE);
    expect(note.querySelector('.usage-note-mark')).toHaveTextContent(ref.textContent!);
    expect(within(sol).queryByText(GPT_NOTE)).toBeNull();
    expect(within(sol).getByText('3 estimated')).toBeInTheDocument();
    // Everything priced or derived from the estimated counts reads as estimated;
    // only the call count is exact.
    const cells = [...sol.querySelectorAll('td')];
    expect(cells.map((td) => td.classList.contains('usage-est'))).toEqual([
      true, true, true, true, true, false,
    ]);

    const opus = container.querySelector<HTMLElement>('tr[data-key="opus5.0"]')!;
    expect(opus.querySelectorAll('td.usage-est')).toHaveLength(0);
    expect(within(opus).queryByTitle(GPT_NOTE)).toBeNull();
    expect(opus.querySelector('th')).not.toHaveAttribute('aria-describedby');
  });

  it('gives each row the colour of its bars, and rows outside the top five the Other colour', async () => {
    const { container } = renderPanel();
    await waitFor(() => expect(rowKeys(container)).toHaveLength(7));
    const swatchOf = (el: Element) => el.querySelector('.usage-swatch')!.className;
    const legend = new Map(
      [...container.querySelectorAll('.usage-legend li')].map((li) => [li.textContent, swatchOf(li)]),
    );
    const rows = [...container.querySelectorAll('#costsBreakdown tbody tr')];
    for (const tr of rows) {
      const name = tr.querySelector('.usage-name-text')!.textContent!;
      expect(swatchOf(tr)).toBe(legend.get(name) ?? legend.get('Other'));
    }
    // Seven rows against five named series: two rows share Other's colour.
    expect(rows.filter((tr) => !legend.has(tr.querySelector('.usage-name-text')!.textContent!))).toHaveLength(2);
  });

  it('sorts by cost, descending, and re-sorts on any column header', async () => {
    const { container } = renderPanel();
    await waitFor(() => expect(rowKeys(container)).toHaveLength(7));
    const header = (name: string) => within(table(container)).getByRole('button', { name });

    expect(rowKeys(container)).toEqual([
      'gpt5.6-sol', 'gpt6-astra', 'opus5.0', 'sonnet5', 'haiku4.5', 'nova-sonic', LOCAL,
    ]);
    expect(header('Cost').closest('th')).toHaveAttribute('aria-sort', 'descending');

    fireEvent.click(header('Cost'));
    expect(rowKeys(container)).toEqual([
      LOCAL, 'nova-sonic', 'haiku4.5', 'sonnet5', 'opus5.0', 'gpt6-astra', 'gpt5.6-sol',
    ]);
    expect(header('Cost').closest('th')).toHaveAttribute('aria-sort', 'ascending');

    fireEvent.click(header('Calls'));
    expect(rowKeys(container)).toEqual([
      'gpt5.6-sol', 'gpt6-astra', 'opus5.0', 'haiku4.5', 'sonnet5', LOCAL, 'nova-sonic',
    ]);
    expect(header('Cost').closest('th')).toHaveAttribute('aria-sort', 'none');

    fireEvent.click(header('Model'));
    expect(rowKeys(container)).toEqual([
      'gpt5.6-sol', 'gpt6-astra', 'haiku4.5', LOCAL, 'nova-sonic', 'opus5.0', 'sonnet5',
    ]);
  });

  it('All time states where it starts, and only All time does', async () => {
    rememberView({ range: 'all' });
    const first = renderPanel();
    await waitFor(() =>
      expect(first.container.querySelector('#costsRangeLine')).toHaveTextContent(
        'Jul 23, 2026 to Sep 23, 2026, UTC days',
      ),
    );
    expect(first.container.querySelector('#costsAllTimeNote')).toHaveTextContent(
      "All time counts from Jul 23, 2026, the app's first public release. Spend recorded before that day is not included.",
    );
    first.unmount();

    const { container } = renderPanel();
    fireEvent.change(screen.getByLabelText('Range'), { target: { value: 'last30' } });
    await waitFor(() =>
      expect(container.querySelector('#costsRangeLine')).toHaveTextContent('Aug 25, 2026 to Sep 23, 2026'),
    );
    expect(container.querySelector('#costsAllTimeNote')).toBeNull();
  });

  it('a range without calls says so over the chart and in the table', async () => {
    // The fixture's quiet week.
    rememberView({ range: 'custom', customFrom: '2026-09-14', customTo: '2026-09-20' });
    const { container } = renderPanel();
    await waitFor(() => expect(container.querySelectorAll('.usage-chart-col')).toHaveLength(7));
    expect(container.querySelector('.usage-chart-empty')).toHaveTextContent('No model calls in this range.');
    expect(container.querySelector('#costsBreakdown')).toBeNull();
  });

  it('a range whose calls all cost $0 explains the flat chart and keeps the tokens in the table', async () => {
    // Local mode: only the on-device model ran, priced at $0.
    getMock.mockImplementation((url: string) => {
      if (!url.startsWith('/api/costs/usage')) return Promise.resolve({});
      const report = usageFor(url);
      const local = report.rows.find((r) => r.key === LOCAL)!;
      report.rows = [{ ...local, share: 0 }];
      report.buckets = report.buckets.map((b) => ({
        ...b,
        cost_usd: 0,
        by_key: Object.fromEntries(Object.entries(b.by_key).filter(([key]) => key === LOCAL)),
      }));
      report.totals = {
        cost_usd: 0,
        prompt_tokens: local.prompt_tokens,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: local.output_tokens,
        calls: local.calls,
        estimated_calls: local.estimated_calls,
        unpriced_calls: 0,
        cache_hit_rate: 0,
        cache_savings_usd: 0,
      };
      return Promise.resolve(report);
    });
    const { container } = renderPanel();
    await waitFor(() => expect(rowKeys(container)).toEqual([LOCAL]));
    expect(container.querySelector('.usage-chart-empty')).toHaveTextContent(
      'Every call in this range cost $0.00, as on-device calls do. The breakdown below lists their tokens.',
    );
    // Token counts read compact in the cell, with the exact count on hover.
    const local = container.querySelector<HTMLElement>(`tr[data-key="${LOCAL}"]`)!;
    expect(local).toHaveTextContent('12.0K');
    expect(within(local).getByTitle(/^12,000 tokens/)).toBeInTheDocument();
  });

  it('never calls a billed but unpriced call free like an on-device one', async () => {
    // A day of Cohere Rerank calls only: counted, no rate in the table.
    const UNPRICED = 'No rate for this model in the pricing table, so its spend shows as $0.';
    getMock.mockImplementation((url: string) => {
      if (!url.startsWith('/api/costs/usage')) return Promise.resolve({});
      const report = usageFor(url);
      const local = report.rows.find((r) => r.key === LOCAL)!;
      const rerank = { ...local, key: 'cohere.rerank-v3-5:0', label: 'cohere.rerank-v3-5:0', estimated_calls: 0, note: UNPRICED };
      report.rows = [{ ...rerank, share: 0 }];
      report.buckets = report.buckets.map((b) => ({ ...b, cost_usd: 0, by_key: {} }));
      report.totals = {
        cost_usd: 0,
        prompt_tokens: rerank.prompt_tokens,
        cache_read_tokens: 0,
        cache_write_tokens: 0,
        output_tokens: 0,
        calls: rerank.calls,
        estimated_calls: 0,
        unpriced_calls: rerank.calls,
        cache_hit_rate: 0,
        cache_savings_usd: 0,
      };
      return Promise.resolve(report);
    });
    const { container } = renderPanel();
    await waitFor(() => expect(rowKeys(container)).toEqual(['cohere.rerank-v3-5:0']));
    const empty = container.querySelector('.usage-chart-empty')!;
    expect(empty).toHaveTextContent('no rate in the pricing table');
    expect(empty).not.toHaveTextContent('on-device');
  });

  it('keeps a deleted session in the breakdown, named as deleted', async () => {
    rememberView({ split: 'session' });
    const { container } = renderPanel();
    await waitFor(() => expect(rowKeys(container)).toEqual(['sess-live', 'sess-gone', 'dream', 'sess-old']));
    const gone = container.querySelector<HTMLElement>('tr[data-key="sess-gone"]')!;
    expect(within(gone).getByText('Deleted session')).toBeInTheDocument();
    expect(within(gone).getByText('sess-gone')).toBeInTheDocument();
    expect(within(table(container)).getByRole('button', { name: 'Session' })).toBeInTheDocument();

    // Spend under an id that never was a saved session keeps the server's name.
    const dream = container.querySelector<HTMLElement>('tr[data-key="dream"]')!;
    expect(within(dream).getByText('Memory dream (not a saved session)')).toBeInTheDocument();
    expect(within(dream).queryByText('Deleted session')).toBeNull();

    // Two deleted sessions in the chart are told apart by their keys.
    expect([...container.querySelectorAll('.usage-legend li')].map((li) => li.textContent)).toEqual([
      'Refactor the importer',
      'Deleted session (sess-gone)',
      'Memory dream (not a saved session)',
      'Deleted session (sess-old)',
    ]);
  });
});

describe('CostsPanel: a wrong answer is an error, not an empty view', () => {
  it('the old /daily shape fails loudly with the reason', async () => {
    getMock.mockImplementation((url: string) =>
      Promise.resolve(url.startsWith('/api/costs/usage') ? { days: [] } : {}),
    );
    const { container } = renderPanel();
    const alert = await screen.findByText(/Could not load usage/);
    expect(alert).toHaveTextContent("the server's usage report does not match what this tab reads");
    expect(container.querySelector('.usage-tiles')).toBeNull();
    expect(container.querySelector('.usage-chart')).toBeNull();
  });

  it('an answer for a different split than the one asked for fails loudly', async () => {
    rememberView({ split: 'session' });
    // A server that ignored the split parameter answers with its default.
    getMock.mockImplementation((url: string) =>
      Promise.resolve(
        url.startsWith('/api/costs/usage') ? usageFor(url.replace('split=session', 'split=model')) : {},
      ),
    );
    const { container } = renderPanel();
    expect(await screen.findByText(/Could not load usage/)).toHaveTextContent(
      'asked for day buckets split by session, but the server answered day buckets split by model.',
    );
    expect(container.querySelector('#costsBreakdown')).toBeNull();
  });

  it('an answer for a different range fails loudly, in the report and in the daily-cap hint', async () => {
    rememberView(FIXTURE_WEEKS);
    // A server that ignored from and to answers its own default range.
    getMock.mockImplementation((url: string) =>
      Promise.resolve(
        url.startsWith('/api/costs/usage')
          ? usageFor(url.replace(/from=[^&]*&to=[^&]*/, 'from=2026-08-25&to=2026-09-23'))
          : {},
      ),
    );
    const { container } = renderPanel();
    expect(await screen.findByText(/Could not load usage/)).toHaveTextContent(
      'asked for 2026-09-01 to 2026-09-23, but the server answered 2026-08-25 to 2026-09-23.',
    );
    expect(container.querySelector('.usage-tiles')).toBeNull();
    // The hint never shows the 30-day total as today's spend.
    await waitFor(() =>
      expect(container.querySelector('#budgetTodaySoFar')).toHaveTextContent(
        'Today so far (UTC day): unavailable (asked for 2026-09-23 to 2026-09-23, but the server answered 2026-08-25 to 2026-09-23.)',
      ),
    );
  });
});
