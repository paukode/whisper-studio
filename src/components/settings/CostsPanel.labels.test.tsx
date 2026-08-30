import { expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CostsPanel } from './CostsPanel';
import { useSettingsStore } from '@/stores/settingsStore';

// Breakdown rows carry internal model KEYS; the panel shows the picker label
// when the key is a known chat model and falls back to the raw key otherwise
// (subagents, removed models). The raw key stays reachable via title=.
vi.mock('@/api/client', () => ({
  get: vi.fn((url: string) => {
    if (url === '/api/costs/models')
      return Promise.resolve({
        models: [
          {
            model: 'local_lmstudio_community_deepseek_r1_0528_qwen3_8b_mlx_4bit__4bit',
            input_tokens: 10,
            output_tokens: 5,
            cost_usd: 0,
          },
          { model: 'unknown_agent', input_tokens: 1, output_tokens: 1, cost_usd: 0 },
        ],
      });
    if (url === '/api/costs/daily') return Promise.resolve({ daily: [] });
    return Promise.resolve({});
  }),
  put: vi.fn(() => Promise.resolve({ updated: true })),
  post: vi.fn(() => Promise.resolve({})),
  del: vi.fn(() => Promise.resolve({})),
}));

it('shows picker labels for known model keys and raw keys otherwise', async () => {
  useSettingsStore.setState({
    models: [
      {
        key: 'local_lmstudio_community_deepseek_r1_0528_qwen3_8b_mlx_4bit__4bit',
        name: 'DeepSeek-R1-0528-Qwen3-8B-MLX-4bit (Local MLX)',
      },
    ] as never,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CostsPanel />
    </QueryClientProvider>,
  );
  await waitFor(() => {
    expect(screen.getByText('DeepSeek-R1-0528-Qwen3-8B-MLX-4bit (Local MLX)')).toBeInTheDocument();
  });
  // The friendly row still carries the raw key for hover/debugging.
  expect(
    screen.getByTitle('local_lmstudio_community_deepseek_r1_0528_qwen3_8b_mlx_4bit__4bit'),
  ).toBeInTheDocument();
  // No label known: the raw key renders as-is.
  expect(screen.getByText('unknown_agent')).toBeInTheDocument();
});
