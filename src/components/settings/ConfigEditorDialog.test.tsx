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
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';

const RAW = {
  user_text: '{\n  "model_mode": "cloud"\n}\n',
  path: '/home/user/config.user.json',
  effective_text:
    '{\n  "model_mode": "cloud",\n  "chat_models": {\n    "opus4.8": { "id": "global.anthropic.claude-opus-4-8" }\n  }\n}\n',
};

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
  it('loads the user config text and path into the editor', async () => {
    renderDialog();
    const box = await screen.findByRole('textbox', { name: 'Your config contents' });
    expect(box).toHaveValue(RAW.user_text);
    expect(screen.getByText(/\/home\/user\/config\.user\.json/)).toBeInTheDocument();
    expect(api.get).toHaveBeenCalledWith('/api/config/raw');
  });

  it('shows the read-only effective (merged) view on its tab, listing shipped models', async () => {
    renderDialog();
    await screen.findByRole('textbox', { name: 'Your config contents' });
    // Switch to the effective tab.
    fireEvent.click(screen.getByRole('tab', { name: 'Effective config (merged)' }));
    const effective = await screen.findByRole('textbox', { name: 'Effective config (merged)' });
    // It is read-only and shows a shipped model the user never declared.
    expect(effective).toHaveAttribute('readonly');
    expect(effective).toHaveValue(RAW.effective_text);
    expect((effective as HTMLTextAreaElement).value).toContain('opus4.8');
    // Save is disabled while viewing the merged (read-only) config.
    expect(screen.getByRole('button', { name: 'Save' })).toBeDisabled();
  });

  it('saves a chat_models_disabled hide-list from the user config', async () => {
    const { qc } = renderDialog();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    const box = await screen.findByRole('textbox', { name: 'Your config contents' });
    const next = '{\n  "chat_models_disabled": ["opus4.8"]\n}\n';
    fireEvent.change(box, { target: { value: next } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() =>
      expect(api.put).toHaveBeenCalledWith('/api/config/raw', { text: next }),
    );
    // The catalog is refreshed so the hidden model drops out of the picker.
    const invalidated = invalidate.mock.calls.map(
      (c) => (c[0] as { queryKey: string[] }).queryKey[0],
    );
    expect(invalidated).toContain('models-manager-catalog');
  });

  it('renders every server validation problem as a list and stays open', async () => {
    const detail =
      'chat_models.tiny: local model is missing a non-empty string "filename" (is_local entries need repo_id, filename and dir to be loadable).\n' +
      '"model_mode" must be one of "cloud", "hybrid" or "local".';
    api.put.mockRejectedValue(new ApiError(400, detail, '/api/config/raw', 'PUT'));
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'Your config contents' });

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
    const box = await screen.findByRole('textbox', { name: 'Your config contents' });
    fireEvent.change(box, { target: { value: '{\n  "a": 1,\n  oops\n}' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    const list = await screen.findByRole('alert');
    expect(list.textContent).toContain('line 3, column 3');
  });

  it('does NOT close on a backdrop (overlay) click', async () => {
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'Your config contents' });
    const overlay = screen.getByRole('dialog', { name: 'Edit your config' });

    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('does NOT close when a selection drag starts in the textarea and releases on the backdrop', async () => {
    const { onClose } = renderDialog();
    const box = await screen.findByRole('textbox', { name: 'Your config contents' });
    const overlay = screen.getByRole('dialog', { name: 'Edit your config' });

    // Press starts inside the editor; the click on release targets the overlay
    // (the mousedown/mouseup common ancestor) — the exact reported trigger.
    fireEvent.mouseDown(box);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('DOES close on the Cancel button', async () => {
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'Your config contents' });
    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('DOES close on Escape', async () => {
    const { onClose } = renderDialog();
    await screen.findByRole('textbox', { name: 'Your config contents' });
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
    await screen.findByRole('textbox', { name: 'Your config contents' });

    fireEvent.click(screen.getByRole('button', { name: 'Cancel' }));

    expect(onClose).toHaveBeenCalledTimes(1);
    expect(parentClick).not.toHaveBeenCalled();
  });

  it('saves, toasts, invalidates the model queries and closes on success', async () => {
    const { onClose, qc } = renderDialog();
    const invalidate = vi.spyOn(qc, 'invalidateQueries');
    const box = await screen.findByRole('textbox', { name: 'Your config contents' });

    const next = '{\n  "chat_models": {}\n}\n';
    fireEvent.change(box, { target: { value: next } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    await waitFor(() => expect(onClose).toHaveBeenCalledTimes(1));
    expect(api.put).toHaveBeenCalledWith('/api/config/raw', { text: next });
    expect(useUIStore.getState().toasts.map((t) => t.message)).toContain('Config saved');
    const invalidated = invalidate.mock.calls.map(
      (c) => (c[0] as { queryKey: string[] }).queryKey[0],
    );
    expect(invalidated).toEqual(
      expect.arrayContaining([
        'models-manager-catalog',
        'models-manager-status',
        'index-engines',
        'models-disabled',
      ]),
    );
  });

  it('refreshes the composer picker store on save, so a new model appears live', async () => {
    // The save added a local model; /api/models now returns it.
    api.get.mockImplementation((url: string) =>
      Promise.resolve(
        url === '/api/models'
          ? {
              models: [
                { key: 'opus4.8', name: 'Opus 4.8' },
                { key: 'tiny_local', name: 'Tiny Test (Local)', is_local: true },
              ],
              default: 'opus4.8',
            }
          : RAW,
      ),
    );
    localStorage.clear();
    useSettingsStore.setState({ models: [{ key: 'opus4.8', name: 'Opus 4.8' }] });
    renderDialog();
    const box = await screen.findByRole('textbox', { name: 'Your config contents' });

    fireEvent.change(box, {
      target: { value: '{\n  "chat_models": {\n    "tiny_local": { "id": "local:tiny" }\n  }\n}\n' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));

    // The store re-fetched /api/models and now offers the new model — the
    // composer picker updates with no app restart.
    await waitFor(() => expect(api.get).toHaveBeenCalledWith('/api/models', expect.anything()));
    await waitFor(() =>
      expect(useSettingsStore.getState().models.map((m) => m.key)).toContain('tiny_local'),
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
    expect(await screen.findByRole('dialog', { name: 'Edit your config' })).toBeInTheDocument();
    // The dialog portals to document.body — never nested inside the <p>.
    expect(screen.getByRole('dialog').closest('p')).toBeNull();
  });
});
