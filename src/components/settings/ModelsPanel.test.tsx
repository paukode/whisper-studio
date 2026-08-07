import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

// The panel talks to /api/models/* through the shared api client; stub it so
// rendering is inert. One model per group plus a downloading and a queued one
// exercise the grouping, the status chips, and the action buttons; the
// absent-with-partial row pins the "Delete is for installed models only" rule.
const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  put: vi.fn(),
}));
vi.mock('@/api/client', () => api);

const CATALOG = {
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
      queue_position: null,
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
      queue_position: null,
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
      queue_position: null,
      error: null,
    },
    {
      key: 'gliner2',
      label: 'GLiNER2 Large v1',
      group: 'indexing',
      repo_id: 'fastino/gliner2-large-v1',
      installed: false,
      size_bytes_estimate: 2_147_483_648,
      bytes_on_disk: 0,
      progress: null, // queued: no run yet, so no progress
      state: 'queued',
      queue_position: 2,
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
      queue_position: null,
      error: null,
    },
  ],
};

import { ModelsPanel } from './ModelsPanel';
import { useUIStore } from '@/stores/uiStore';

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockImplementation((url: string) => {
    if (url === '/api/models/catalog') return Promise.resolve(CATALOG);
    // The chat-model visibility section fetches its own list shape.
    if (url === '/api/config/models-disabled')
      return Promise.resolve({
        disabled: [],
        models: [{ key: 'opus4.8', label: 'Opus 4.8', is_local: false, disabled: false }],
      });
    return Promise.resolve({ models: {} });
  });
  api.post.mockResolvedValue({});
  api.del.mockResolvedValue({ deleted: true });
  useUIStore.setState({ toasts: [] });
});

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

    // Queued row: position chip + its own Cancel (dequeues server-side).
    expect(screen.getByText('Queued (2nd)')).toBeInTheDocument();
    expect(screen.getByText(/starts when a slot frees/)).toBeInTheDocument();
    expect(screen.getAllByRole('button', { name: 'Cancel' })).toHaveLength(2);
  });

  it('cancels a queued row through the normal cancel endpoint', async () => {
    renderPanel();
    await screen.findByText('Queued (2nd)');

    const cancels = screen.getAllByRole('button', { name: 'Cancel' });
    // Rows render in group order: the queued GLiNER2 (indexing) comes before
    // the downloading Gemma (local-chat).
    fireEvent.click(cancels[0]);
    await waitFor(() => expect(api.post).toHaveBeenCalledWith('/api/models/gliner2/cancel'));
  });

  it('shows an info toast (not an error) when a download gets queued', async () => {
    api.post.mockResolvedValue({
      started: false,
      queued: true,
      key: 'gliner',
      state: 'queued',
      queue_position: 2,
    });
    renderPanel();
    await screen.findByText('GLiNER Large v2.5');

    // Both Download buttons: parakeet (transcription) then gliner (indexing).
    fireEvent.click(screen.getAllByRole('button', { name: 'Download' })[1]);
    await waitFor(() => {
      const toasts = useUIStore.getState().toasts;
      expect(toasts).toHaveLength(1);
      // 1 already downloading + 1 ahead in the queue = behind 2 downloads.
      expect(toasts[0].message).toBe('GLiNER Large v2.5 queued behind 2 downloads');
      expect(toasts[0].type).toBe('info');
    });
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

  it('makes a failed download recoverable: Error message, Retry, and Delete', async () => {
    // A local chat model whose download died: this is the reported bug's row.
    api.get.mockImplementation((url: string) =>
      url === '/api/models/catalog'
        ? Promise.resolve({
            models: [
              {
                key: 'local_qwen',
                label: 'Qwen3.6 27B (Local)',
                group: 'local-chat',
                repo_id: 'unsloth/Qwen3.6-27B-GGUF',
                installed: false,
                size_bytes_estimate: 16_000_000_000,
                bytes_on_disk: 512_000, // a dead attempt left partials
                progress: 0,
                state: 'error',
                error: 'Download failed (with exit code 1): repository not found on Hugging Face',
                queue_position: null,
              },
            ],
          })
        : Promise.resolve({ models: {} }),
    );
    renderPanel();
    await screen.findByText('Qwen3.6 27B (Local)');

    // The error chip and the expandable message both surface the reason.
    expect(screen.getByText('Error')).toBeInTheDocument();
    expect(screen.getByText(/repository not found on Hugging Face/)).toBeInTheDocument();

    // Retry re-POSTs the download (the backend clears the dead job and restarts).
    fireEvent.click(screen.getByRole('button', { name: 'Retry' }));
    await waitFor(() =>
      expect(api.post).toHaveBeenCalledWith('/api/models/local_qwen/download'),
    );

    // The "…" overflow menu offers "Delete downloaded files" (error + partials):
    // it opens the confirm dialog, then clears the stranded partials server-side.
    fireEvent.click(screen.getByRole('button', { name: /More actions for/i }));
    fireEvent.click(await screen.findByText('Delete downloaded files'));
    expect(await screen.findByText('Delete Qwen3.6 27B (Local)?')).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Delete' })); // the dialog's confirm
    await waitFor(() => expect(api.del).toHaveBeenCalledWith('/api/models/local_qwen'));
  });

  it('local-chat overflow menu: installed row offers delete files + remove from list', async () => {
    api.get.mockImplementation((url: string) =>
      url === '/api/models/catalog'
        ? Promise.resolve({
            models: [
              {
                key: 'local_qwen',
                label: 'Qwen3 8B (Local)',
                group: 'local-chat',
                repo_id: 'unsloth/Qwen3-8B-GGUF',
                installed: true,
                size_bytes_estimate: 5_000_000_000,
                bytes_on_disk: 5_000_000_000,
                progress: 1,
                state: 'installed',
                queue_position: null,
                error: null,
              },
            ],
          })
        : url === '/api/config/models-disabled'
          ? Promise.resolve({ disabled: [], models: [] })
          : Promise.resolve({ models: {} }),
    );
    renderPanel();
    await screen.findByText('Qwen3 8B (Local)');

    // No bare Delete button on the row anymore — it lives in the "…" menu.
    expect(screen.queryByRole('button', { name: 'Delete' })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /More actions for/i }));
    // Installed → both actions are offered.
    expect(await screen.findByText('Delete downloaded files')).toBeInTheDocument();
    expect(screen.getByText('Remove from list')).toBeInTheDocument();
  });

  it('local-chat overflow menu: a not-downloaded row offers only remove from list', async () => {
    api.get.mockImplementation((url: string) =>
      url === '/api/models/catalog'
        ? Promise.resolve({
            models: [
              {
                key: 'local_ministral',
                label: 'Ministral 8B (Local)',
                group: 'local-chat',
                repo_id: 'mistralai/Ministral-8B-GGUF',
                installed: false,
                size_bytes_estimate: 5_000_000_000,
                bytes_on_disk: 0,
                progress: 0,
                state: 'absent',
                queue_position: null,
                error: null,
              },
            ],
          })
        : url === '/api/config/models-disabled'
          ? Promise.resolve({ disabled: [], models: [] })
          : Promise.resolve({ models: {} }),
    );
    renderPanel();
    await screen.findByText('Ministral 8B (Local)');

    // Download is still offered for a not-downloaded model.
    expect(screen.getByRole('button', { name: 'Download' })).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: /More actions for/i }));
    // Nothing on disk → no "Delete downloaded files", only "Remove from list".
    expect(await screen.findByText('Remove from list')).toBeInTheDocument();
    expect(screen.queryByText('Delete downloaded files')).not.toBeInTheDocument();
  });

  it('remove from list hits the entry endpoint and toasts the local re-download hint', async () => {
    api.get.mockImplementation((url: string) =>
      url === '/api/models/catalog'
        ? Promise.resolve({
            models: [
              {
                key: 'local_gemma',
                label: 'Gemma 4 12B (Local)',
                group: 'local-chat',
                repo_id: 'google/gemma-4-12B-it-qat-q4_0-gguf',
                installed: false,
                size_bytes_estimate: 6_000_000_000,
                bytes_on_disk: 0,
                progress: 0,
                state: 'absent',
                queue_position: null,
                error: null,
              },
            ],
          })
        : url === '/api/config/models-disabled'
          ? Promise.resolve({ disabled: [], models: [] })
          : Promise.resolve({ models: {} }),
    );
    // A local model → the backend reports scope "local" (deleted outright).
    api.del.mockResolvedValue({
      key: 'local_gemma',
      scope: 'local',
      removed_entry: true,
      weights_deleted: false,
      stopped_llama_server: false,
    });
    renderPanel();
    await screen.findByText('Gemma 4 12B (Local)');

    fireEvent.click(screen.getByRole('button', { name: /More actions for/i }));
    fireEvent.click(await screen.findByText('Remove from list'));
    // Confirm dialog, then the dedicated remove-entry endpoint is called.
    expect(await screen.findByText(/Remove Gemma 4 12B \(Local\) from the list\?/)).toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'Remove' }));
    await waitFor(() =>
      expect(api.del).toHaveBeenCalledWith('/api/models/browse/local_gemma/entry'),
    );
    await waitFor(() => {
      const toasts = useUIStore.getState().toasts;
      expect(toasts.some((t) => /Discover/.test(t.message))).toBe(true);
    });
  });

  it('shows a local-chat model even if it appears in the Bedrock hide-list', async () => {
    // Local models are deleted, never tombstoned, so the chat_models_disabled
    // list must NOT hide a local-chat row (that list is Bedrock-only now).
    api.get.mockImplementation((url: string) =>
      url === '/api/models/catalog'
        ? Promise.resolve({
            models: [
              {
                key: 'local_gemma',
                label: 'Gemma 4 12B (Local)',
                group: 'local-chat',
                repo_id: 'google/gemma-4-12B-it-qat-q4_0-gguf',
                installed: false,
                size_bytes_estimate: 6_000_000_000,
                bytes_on_disk: 0,
                progress: 0,
                state: 'absent',
                queue_position: null,
                error: null,
              },
            ],
          })
        : url === '/api/config/models-disabled'
          ? Promise.resolve({ disabled: ['local_gemma'], models: [] })
          : Promise.resolve({ models: {} }),
    );
    renderPanel();
    // The Local chat row still renders despite being in the hide-list.
    expect(await screen.findByText('Gemma 4 12B (Local)')).toBeInTheDocument();
  });
});
