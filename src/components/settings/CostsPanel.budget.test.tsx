import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor, fireEvent } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CostsPanel } from './CostsPanel';

// The panel reads/writes budget settings via the api client. Route GET by URL
// so /api/config seeds the form while the cost endpoints stay empty.
const CONFIG = {
  max_session_cost_usd: 3.5,
  max_daily_cost_usd: 12,
  model_fallback_enabled: true,
};

vi.mock('@/api/client', () => ({
  get: vi.fn((url: string) => {
    if (url === '/api/config') return Promise.resolve(CONFIG);
    if (url === '/api/costs/models') return Promise.resolve({ models: [] });
    if (url === '/api/costs/daily') return Promise.resolve({ daily: [] });
    // /api/costs/summary and anything else
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

describe('CostsPanel — budget save wiring', () => {
  beforeEach(() => vi.clearAllMocks());

  it('seeds the budget fields from GET /api/config on mount', async () => {
    const { container } = renderPanel();

    const session = () => container.querySelector<HTMLInputElement>('#budgetMaxSession')!;
    const daily = () => container.querySelector<HTMLInputElement>('#budgetMaxDaily')!;
    const fallback = () => container.querySelector<HTMLInputElement>('#budgetModelFallback')!;

    await waitFor(() => expect(session().value).toBe('3.5'));
    expect(daily().value).toBe('12');
    expect(fallback().checked).toBe(true);
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

  it('shows "Saved!" after a successful save', async () => {
    const { container } = renderPanel();
    await waitFor(() =>
      expect(container.querySelector<HTMLInputElement>('#budgetMaxSession')!.value).toBe('3.5'),
    );

    fireEvent.click(screen.getByRole('button', { name: /save budget/i }));
    await waitFor(() => expect(screen.getByText('Saved!')).toBeTruthy());
  });
});
