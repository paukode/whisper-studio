import { expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CostsPanel } from './CostsPanel';
import { useSettingsStore } from '@/stores/settingsStore';
import { usageFor } from '@/test/fixtures/costsUsage';

// Breakdown rows carry the model KEY and the server's label. The server's
// label wins; when the server knows no friendly name (it sends the key back as
// the label, as for an on-device model), the model picker's name fills in.
// The raw key stays reachable via title= either way.
vi.mock('@/api/client', () => ({
  get: vi.fn((url: string) =>
    Promise.resolve(url.startsWith('/api/costs/usage') ? usageFor(url) : {}),
  ),
  put: vi.fn(() => Promise.resolve({ updated: true })),
  post: vi.fn(() => Promise.resolve({})),
  del: vi.fn(() => Promise.resolve({})),
}));

const LOCAL = 'local_lmstudio_community_deepseek_r1_0528_qwen3_8b_mlx_4bit__4bit';

it('shows server labels, picker names for keys the server cannot name, raw keys on hover', async () => {
  useSettingsStore.setState({
    models: [
      { key: LOCAL, name: 'DeepSeek-R1-0528-Qwen3-8B-MLX-4bit (Local MLX)' },
      // A picker name never overrides a label the server sent.
      { key: 'opus5.0', name: 'Picker name for Opus' },
    ] as never,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CostsPanel />
    </QueryClientProvider>,
  );
  const table = await waitFor(() => {
    const el = document.querySelector<HTMLElement>('#costsBreakdown');
    expect(el).not.toBeNull();
    return el!;
  });
  const local = table.querySelector(`tr[data-key="${LOCAL}"] .usage-name-text`)!;
  expect(local).toHaveTextContent('DeepSeek-R1-0528-Qwen3-8B-MLX-4bit (Local MLX)');
  expect(local).toHaveAttribute('title', LOCAL);

  const opus = table.querySelector('tr[data-key="opus5.0"] .usage-name-text')!;
  expect(opus).toHaveTextContent('Opus 5');
  expect(opus).toHaveAttribute('title', 'opus5.0');
  expect(screen.queryByText('Picker name for Opus')).toBeNull();
});
