import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// The browser talks to /api/models/browse/* through the shared api client; stub
// it so rendering is inert and we can assert the exact install payload.
const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  put: vi.fn(),
  patch: vi.fn(),
}));
vi.mock('@/api/client', () => api);

const SEARCH = {
  count: 1,
  results: [
    {
      repo_id: 'unsloth/Qwen3-0.6B-GGUF',
      author: 'unsloth',
      name: 'Qwen3-0.6B-GGUF',
      label: 'Qwen3-0.6B-GGUF',
      downloads: 308619,
      likes: 42,
      trending_score: 5,
      arch: 'qwen3',
      param_size: '0.6B',
      context_length: 40960,
      gated: false,
      supported: true,
    },
  ],
};

const REPO_DETAIL = {
  repo_id: 'unsloth/Qwen3-0.6B-GGUF',
  arch: 'qwen3',
  supported: true,
  context_length: 40960,
  has_chat_template: true,
  gated: false,
  recommended_filename: 'Qwen3-0.6B-Q4_K_M.gguf',
  mem_total_bytes: 18_000_000_000,
  mem_budget_bytes: 9_500_000_000,
  mem_reserved_bytes: 4_000_000_000,
  quants: [
    {
      quant: 'Q2_K',
      filename: 'Qwen3-0.6B-Q2_K.gguf',
      size_bytes: 296_238_784,
      is_sharded: false,
      shard_count: 1,
      recommended: false,
      fit: 'ok',
    },
    {
      quant: 'Q4_K_M',
      filename: 'Qwen3-0.6B-Q4_K_M.gguf',
      size_bytes: 396_705_472,
      is_sharded: false,
      shard_count: 1,
      recommended: true,
      fit: 'ok',
    },
  ],
};

// A repo whose only realistic quant is too large for the running machine, so
// the API returns a 'too_big' verdict alongside the memory numbers used to
// word the warning.
const OVERSIZED_REPO_DETAIL = {
  repo_id: 'unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF',
  arch: 'qwen3moe',
  supported: true,
  context_length: 262144,
  has_chat_template: true,
  gated: false,
  recommended_filename: 'Qwen3-Coder-30B-Q4_K_M.gguf',
  mem_total_bytes: 18_000_000_000, // renders as "18.0 GB" via humanSize
  mem_budget_bytes: 9_500_000_000, // renders as "9.5 GB" via humanSize
  mem_reserved_bytes: 4_000_000_000,
  quants: [
    {
      quant: 'Q4_K_M',
      filename: 'Qwen3-Coder-30B-Q4_K_M.gguf',
      size_bytes: 19_972_876_288, // ~18.6 GiB — the failure this feature exists for
      is_sharded: false,
      shard_count: 1,
      recommended: true,
      fit: 'too_big',
      needed_bytes: 22_120_359_936, // size + 2 GiB runtime overhead, renders "22.1 GB"
    },
  ],
};

import { ModelBrowser } from './ModelBrowser';
import { useUIStore } from '@/stores/uiStore';

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockImplementation((url: string) => {
    if (url.startsWith('/api/models/browse/search')) return Promise.resolve(SEARCH);
    if (url.startsWith('/api/models/browse/repo/')) return Promise.resolve(REPO_DETAIL);
    if (url === '/api/models') return Promise.resolve({ models: [], default: '' });
    return Promise.resolve({});
  });
  api.post.mockResolvedValue({
    key: 'local_unsloth_qwen3_0_6b_gguf__q4_k_m',
    id: 'local:unsloth-qwen3-0-6b-gguf-q4-k-m',
    label: 'Qwen3-0.6B-GGUF Q4_K_M (Local)',
    ctx: 32768,
    supports_thinking: true,
    supports_tools: true,
    download: 'started',
  });
  useUIStore.setState({ toasts: [] });
});

function renderBrowser() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelBrowser />
    </QueryClientProvider>,
  );
}

describe('ModelBrowser (Discover)', () => {
  it('shows search results with author, param size, and download count', async () => {
    renderBrowser();
    // The default trending search runs on mount.
    expect(await screen.findByText('Qwen3-0.6B-GGUF')).toBeInTheDocument();
    expect(screen.getByText('unsloth')).toBeInTheDocument();
    expect(screen.getByText('0.6B')).toBeInTheDocument();
    expect(screen.getByText(/309K downloads/)).toBeInTheDocument();
  });

  it('fetches quants on first interaction with the dropdown and pre-selects the recommended one', async () => {
    renderBrowser();
    await screen.findByText('Qwen3-0.6B-GGUF');

    const select = screen.getByRole('combobox', {
      name: /choose a quant for Qwen3-0\.6B-GGUF/i,
    }) as HTMLSelectElement;
    // Before interaction the detail is not fetched — just a placeholder option.
    expect(api.get).not.toHaveBeenCalledWith(
      expect.stringContaining('/api/models/browse/repo/'),
    );
    // Download is disabled until the quants load.
    expect(screen.getByRole('button', { name: 'Download' })).toBeDisabled();

    // Focusing the select triggers the lazy fetch (the old "Choose quant" step).
    fireEvent.focus(select);

    // Both quants become options; the recommended one is labelled and preselected.
    await waitFor(() => expect(select.value).toBe('Qwen3-0.6B-Q4_K_M.gguf'));
    expect(screen.getByRole('option', { name: /Q4_K_M.*\(recommended\)/i })).toBeInTheDocument();
    expect(screen.getByRole('option', { name: /Q2_K/ })).toBeInTheDocument();

    // Download now installs the pre-selected recommended file.
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/models/browse/install', {
        repo_id: 'unsloth/Qwen3-0.6B-GGUF',
        filename: 'Qwen3-0.6B-Q4_K_M.gguf',
      }),
    );
    // A success toast is shown after a started download.
    await waitFor(() => {
      const toasts = useUIStore.getState().toasts;
      expect(toasts.some((t) => t.type === 'success')).toBe(true);
    });
  });

  it('lets the user pick a different quant from the dropdown before installing', async () => {
    renderBrowser();
    await screen.findByText('Qwen3-0.6B-GGUF');

    const select = screen.getByRole('combobox', {
      name: /choose a quant for Qwen3-0\.6B-GGUF/i,
    }) as HTMLSelectElement;
    fireEvent.focus(select);
    await waitFor(() => expect(select.value).toBe('Qwen3-0.6B-Q4_K_M.gguf'));

    // Choose the smaller Q2_K, then install — the payload must follow the pick.
    fireEvent.change(select, { target: { value: 'Qwen3-0.6B-Q2_K.gguf' } });
    fireEvent.click(screen.getByRole('button', { name: 'Download' }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/models/browse/install', {
        repo_id: 'unsloth/Qwen3-0.6B-GGUF',
        filename: 'Qwen3-0.6B-Q2_K.gguf',
      }),
    );
  });

  it('browses an empty-term default list for BOTH sorts and refetches on sort change', async () => {
    renderBrowser();
    // Mount runs the default trending browse with NO search term.
    await screen.findByText('Qwen3-0.6B-GGUF');
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/models\/browse\/search\?.*sort=trending/),
      ),
    );
    // The empty-term browse must NOT include a q param.
    expect(api.get).not.toHaveBeenCalledWith(expect.stringContaining('q='));

    // Switching to Most downloaded refetches with the new sort, still empty term.
    fireEvent.change(screen.getByRole('combobox', { name: /sort order/i }), {
      target: { value: 'downloads' },
    });
    await waitFor(() =>
      expect(api.get).toHaveBeenCalledWith(
        expect.stringMatching(/\/api\/models\/browse\/search\?.*sort=downloads/),
      ),
    );
    // Results still render (the query is no longer gated behind a search term).
    expect(await screen.findByText('Qwen3-0.6B-GGUF')).toBeInTheDocument();
  });

  it("warns and offers 'Download anyway' when the selected quant is too big for this machine", async () => {
    const OVERSIZED_SEARCH = {
      count: 1,
      results: [
        {
          repo_id: 'unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF',
          author: 'unsloth',
          name: 'Qwen3-Coder-30B-A3B-Instruct-GGUF',
          label: 'Qwen3-Coder-30B-A3B-Instruct-GGUF',
          downloads: 12345,
          likes: 1,
          trending_score: 1,
          arch: 'qwen3moe',
          param_size: '30B',
          context_length: 262144,
          gated: false,
          supported: true,
        },
      ],
    };
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/models/browse/search')) return Promise.resolve(OVERSIZED_SEARCH);
      if (url.startsWith('/api/models/browse/repo/'))
        return Promise.resolve(OVERSIZED_REPO_DETAIL);
      if (url === '/api/models') return Promise.resolve({ models: [], default: '' });
      return Promise.resolve({});
    });

    renderBrowser();
    await screen.findByText('Qwen3-Coder-30B-A3B-Instruct-GGUF');

    const select = screen.getByRole('combobox', {
      name: /choose a quant for Qwen3-Coder-30B-A3B-Instruct-GGUF/i,
    }) as HTMLSelectElement;
    fireEvent.focus(select);
    await waitFor(() => expect(select.value).toBe('Qwen3-Coder-30B-Q4_K_M.gguf'));

    // The option itself is flagged, and a plain-language warning line appears
    // stating the model's own requirement plus the memory numbers the API
    // returned for this machine.
    expect(screen.getByRole('option', { name: /won't fit/i })).toBeInTheDocument();
    expect(screen.getByRole('alert')).toHaveTextContent(/Needs about/i);
    expect(screen.getByRole('alert')).toHaveTextContent(/22\.1 GB/); // needed_bytes
    expect(screen.getByRole('alert')).toHaveTextContent(/9\.5 GB/); // mem_budget_bytes

    // The button still allows the download, worded as an informed choice.
    const button = screen.getByRole('button', { name: 'Download anyway' });
    fireEvent.click(button);
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/models/browse/install', {
        repo_id: 'unsloth/Qwen3-Coder-30B-A3B-Instruct-GGUF',
        filename: 'Qwen3-Coder-30B-Q4_K_M.gguf',
      }),
    );
  });

  it("badges a recommended model that won't fit this machine's memory budget", async () => {
    const RECOMMENDED = {
      count: 1,
      recommended: [
        {
          key: 'local_test_oversized',
          repo_id: 'test-org/Test-Model-GGUF',
          author: 'test-org',
          name: 'Test-Model-GGUF',
          label: 'Test Model (Local)',
          filename: 'Test-Model-Q4_K_M.gguf',
          quant: 'Q4_K_M',
          param_size: '30B',
          ctx: 32768,
          supports_thinking: true,
          supports_tools: true,
          downloaded: false,
          size_bytes: 19_972_876_288,
          fit: 'too_big',
          needed_bytes: 22_120_359_936,
        },
      ],
    };
    api.get.mockImplementation((url: string) => {
      if (url.startsWith('/api/models/browse/search')) return Promise.resolve(SEARCH);
      if (url.startsWith('/api/models/browse/repo/')) return Promise.resolve(REPO_DETAIL);
      if (url.startsWith('/api/models/browse/recommended')) return Promise.resolve(RECOMMENDED);
      if (url === '/api/models') return Promise.resolve({ models: [], default: '' });
      return Promise.resolve({});
    });

    renderBrowser();
    expect(await screen.findByText('Test Model (Local)')).toBeInTheDocument();
    expect(screen.getByText(/won't fit on this machine/i)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Download anyway' })).toBeInTheDocument();
  });
});
