import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { ApiError } from '@/types/api';

// The dialog talks through the shared api client; stub it so every request is
// programmable (same approach as ModelsPanel.test.tsx).
const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  put: vi.fn(),
}));
vi.mock('@/api/client', () => api);

import { ConfigEditorDialog, ConfigJsonLink } from './ConfigEditorDialog';
import { useUIStore } from '@/stores/uiStore';

const RAW = { text: '{\n  "model_mode": "cloud"\n}\n', path: '/home/user/config.json' };

function renderDialog(onClose = vi.fn()) {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <ConfigEditorDialog onClose={onClose} />
    </QueryClientProvider>,
  );
  return { onClose, qc };
}

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue(RAW);
  api.put.mockResolvedValue({ ok: true, path: RAW.path, local_models: ['local_gemma'] });
  useUIStore.setState({ toasts: [] });
});

describe('ConfigEditorDialog', () => {
  it('loads the current config.json text and path into the editor', async () => {
    renderDialog();
    const box = await screen.findByRole('textbox', { name: 'config.json contents' });
    expect(box).toHaveValue(RAW.text);
    expect(screen.getByText(/\/home\/user\/config\.json/)).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/config/raw');
  });

  it('renders every server validation problem as a list and stays open', async () => {
    const detail =
      'chat_models.tiny: local model is missing a non-empty string "filename" (is_local entries need repo_id, filename and dir to be loadable).\n' +
      '"model_mode" must be one of "cloud", "hybrid" or "local".';
    api.put.mockRejectedValue(new ApiError(400, detail, '/api/config/raw', 'PUT'));
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'config.json contents' });

    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const list = await screen.findByRole('alert');
    const items = list.querySelectorAll('li');
    expect(items).toHaveLength(2);
    expect(items[0].textContent).toContain('chat_models.tiny');
    expect(items[1].textContent).toContain('"model_mode"');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('shows the syntax error with line and column from the server', async () => {
    api.put.mockRejectedValue(
      new ApiError(
        400,
        'Not valid JSON at line 3, column 3: Expecting property name enclosed in double quotes.',
        '/api/config/raw',
        'PUT',
      ),
    );
    renderDialog();
    const box = await screen.findByRole('textbox', { name: 'config.json contents' });
    fireEvent.change(box, { target: { value: '{\n  "a": 1,\n  oops\n}' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const list = await screen.findByRole('alert');
    expect(list.textContent).toContain('line 3, column 3');
  });

  it('does NOT close on a backdrop (overlay) click', async () => {
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'config.json contents' });
    const overlay = screen.getByRole('dialog', { name: 'Edit config.json' });

    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('does NOT close when a selection drag starts in the textarea and releases on the backdrop', async () => {
    const { onClose } = renderDialog();
    const box = await screen.findByRole('textbox', { name: 'config.json contents' });
    const overlay = screen.getByRole('dialog', { name: 'Edit config.json' });

    // Press starts inside the editor; the click on release targets the overlay
    // (the mousedown/mouseup common ancestor) — the exact reported trigger.
    fireEvent.mouseDown(box);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('DOES close on the Cancel button', async () => {
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'config.json contents' });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('DOES close on Escape', async () => {
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'config.json contents' });
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('closing (Cancel) does not bubble to the hosting Settings modal', async () => {
    const onClose = vi.fn();
    const parentClick = vi.fn();
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    // Simulate the real nesting: the editor portals to <body> but its React
    // parent chain runs through the Settings modal, so a stray bubbling click
    // could reach a Settings-level handler. The dialog's own stopPropagation
    // must keep its clicks from ever reaching that parent.
    render(
      <QueryClientProvider client={qc}>
        <div data-testid="settings-parent" onClick={parentClick}>
          <ConfigEditorDialog onClose={onClose} />
        </div>
      </QueryClientProvider>,
    );
    await screen.findByRole('textbox', { name: 'config.json contents' });

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(parentClick).not.toHaveBeenCalled();
  });

  it('saves, toasts, invalidates the model queries and closes on success', async () => {
    const { onClose, qc } = renderDialog();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    const box = await screen.findByRole('textbox', { name: 'config.json contents' });

    const next = '{\n  "chat_models": {}\n}\n';
    fireEvent.change(box, { target: { value: next } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith('/api/config/raw', { text: next });
    expect(useUIStore.getState().toasts.map((t) => t.message)).toContain('config.json saved');
    const invalidated = invalidate.mock.calls.map(
      (c) => (c[0] as { queryKey: string[] }).queryKey[0],
    );
    expect(invalidated).toEqual(
      expect.arrayContaining(['models-manager-catalog', 'models-manager-status', 'index-engines']),
    );
  });
});

describe('ConfigJsonLink', () => {
  it('opens the editor dialog from the inline link', async () => {
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <p>
          Add models by editing <ConfigJsonLink />.
        </p>
      </QueryClientProvider>,
    );
    expect(screen.queryByRole('dialog')).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole('button', { name: 'config.json' }));
    expect(await screen.findByRole('dialog', { name: 'Edit config.json' })).toBeInTheDocument();
    // The dialog portals to document.body — never nested inside the <p>.
    expect(screen.getByRole('dialog').closest('p')).toBeNull();
  });
});
