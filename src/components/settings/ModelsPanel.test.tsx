import { describe, expect, it, vi } from 'vitest';
import { render, screen } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// The panel talks to /api/models/* through the shared api client; stub it so
// rendering is inert. One model per group plus a downloading one exercises the
// grouping, the status chips, and the action buttons; the absent-with-partial
// row pins the "Delete is for installed models only" rule.
vi.mock('@/api/client', () => ({
  get: vi.fn((url: string) => {
    if (url === '/api/models/catalog') {
      return Promise.resolve({
        models: [
          {
            key: 'whisper',
            label: 'Whisper Large v3 Turbo',
            group: 'transcription',
            repo_id: 'mlx-community/whisper-large-v3-turbo',
            installed: true,
            size_bytes_estimate: 1_613_980_376,
            bytes_on_disk: 1_613_980_376,
            progress: 1,
            state: 'installed',
            error: null,
          },
          {
            key: 'parakeet',
            label: 'Parakeet TDT 0.6B v3',
            group: 'transcription',
            repo_id: 'mlx-community/parakeet-tdt-0.6b-v3',
            installed: false,
            size_bytes_estimate: 2_509_045_082,
            bytes_on_disk: 756_000, // a cancelled attempt left hf resume files
            progress: 0.0003,
            state: 'absent',
            error: null,
          },
          {
            key: 'gliner',
            label: 'GLiNER Large v2.5',
            group: 'indexing',
            repo_id: 'gliner-community/gliner_large-v2.5',
            installed: false,
            size_bytes_estimate: 3_687_509_485,
            bytes_on_disk: 0,
            progress: 0,
            state: 'absent',
            error: null,
          },
          {
            key: 'local_gemma',
            label: 'Gemma 4 12B (Local)',
            group: 'local-chat',
            repo_id: 'google/gemma-4-12B-it-qat-q4_0-gguf',
            installed: false,
            size_bytes_estimate: 6_975_879_296,
            bytes_on_disk: 3_487_939_648, // raw ratio would say 50%...
            progress: 0.62, // ...but the server's shared number must win
            state: 'downloading',
            error: null,
          },
        ],
      });
    }
    return Promise.resolve({ models: {} });
  }),
  post: vi.fn().mockResolvedValue({}),
  del: vi.fn().mockResolvedValue({ deleted: true }),
  put: vi.fn(),
}));

import { ModelsPanel } from './ModelsPanel';

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  return render(
    <QueryClientProvider client={qc}>
      <ModelsPanel />
    </QueryClientProvider>,
  );
}

describe('ModelsPanel', () => {
  it('renders the three groups with status chips and the right actions', async () => {
    renderPanel();

    // Group headers appear once the catalog resolves.
    expect(await screen.findByText('Transcription')).toBeInTheDocument();
    expect(screen.getByText('Indexing')).toBeInTheDocument();
    expect(screen.getByText('Local chat')).toBeInTheDocument();

    // Installed row: chip + Delete.
    expect(screen.getByText('Whisper Large v3 Turbo')).toBeInTheDocument();
    expect(screen.getByText('Installed')).toBeInTheDocument();

    // Absent row: human size + Download.
    expect(screen.getByText(/3\.7 GB download/)).toBeInTheDocument();

    // Downloading row: the SERVER-computed percent (62%), never the local
    // bytes/estimate ratio (which would read 50%) + Cancel.
    expect(screen.getByText(/Downloading 62%/)).toBeInTheDocument();
    expect(screen.queryByText(/Downloading 50%/)).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Cancel' })).toBeInTheDocument();
  });

  it('shows Delete only for installed models — a partial download keeps just Download', async () => {
    renderPanel();
    await screen.findByText('Transcription');

    // Exactly one Delete: the installed Whisper row. The absent Parakeet row
    // with partial bytes on disk must NOT offer Delete...
    expect(screen.getAllByRole('button', { name: 'Delete' })).toHaveLength(1);

    // ...but it still surfaces the partial bytes as info text, and offers
    // Download (which resumes from those bytes server-side).
    expect(screen.getByText(/756 KB partial on disk/)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Download' })).toHaveLength(2);
  });
});
