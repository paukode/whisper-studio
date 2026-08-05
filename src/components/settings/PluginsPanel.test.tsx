import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const api = vi.hoisted(() => ({ get: vi.fn() }));
vi.mock('@/api/client', () => api);

import { PluginsPanel } from './PluginsPanel';

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <PluginsPanel />
    </QueryClientProvider>,
  );
  return qc;
}

const DISABLED_LIST = {
  plugins: [
    { name: 'security_checks', description: 'Safety', enabled: true, protected: true, version: '1.0.0', tools: [] },
    { name: 'e2e_plug', description: 'E2E ping plugin', enabled: false, protected: false, version: '2.3.4', tools: [] },
  ],
  plugins_dir: '/home/user/plugins',
};

const ENABLED_LIST = {
  plugins: [
    { name: 'security_checks', description: 'Safety', enabled: true, protected: true, version: '1.0.0', tools: [] },
    { name: 'e2e_plug', description: 'E2E ping plugin', enabled: true, protected: false, version: '2.3.4', tools: ['e2e_ping'] },
  ],
  plugins_dir: '/home/user/plugins',
};

beforeEach(() => {
  vi.clearAllMocks();
  vi.unstubAllGlobals();
});

describe('PluginsPanel', () => {
  it('lists plugins, locks the protected one on, and shows the folder', async () => {
    api.get.mockResolvedValue(DISABLED_LIST);
    renderPanel();

    expect(await screen.findByText('e2e_plug')).toBeInTheDocument();
    expect(screen.getByText('/home/user/plugins')).toBeInTheDocument();
    expect(screen.getByText('REQUIRED')).toBeInTheDocument();

    // The protected plugin's switch is checked + disabled; the other is off.
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    const protectedBox = boxes.find((b) => b.disabled);
    expect(protectedBox?.checked).toBe(true);
  });

  it('uploads a .py file to the upload endpoint and refetches', async () => {
    api.get.mockResolvedValue(DISABLED_LIST);
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({ name: 'e2e_plug', uploaded: true, enabled: false }),
    }));
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    renderPanel();
    await screen.findByText('e2e_plug');

    const input = screen.getByLabelText('Add plugin file') as HTMLInputElement;
    const file = new File(['def register(a,b):\n    pass\n'], 'e2e_plug.py', { type: 'text/x-python' });
    Object.defineProperty(input, 'files', { value: [file], configurable: true });
    fireEvent.change(input);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith('/api/plugins/upload', expect.objectContaining({ method: 'POST' })),
    );
    const body = (fetchMock.mock.calls[0][1] as unknown as { body: FormData }).body;
    expect(body).toBeInstanceOf(FormData);
    expect((body.get('file') as File).name).toBe('e2e_plug.py');
  });

  it('reveals a valid, em-dash-free plugin template on demand', async () => {
    api.get.mockResolvedValue(DISABLED_LIST);
    const { container } = (() => {
      const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
      return render(
        <QueryClientProvider client={qc}>
          <PluginsPanel />
        </QueryClientProvider>,
      );
    })();

    await screen.findByText('e2e_plug');

    // The template is hidden until the user asks for it.
    expect(screen.queryByText(/def register\(/)).not.toBeInTheDocument();

    fireEvent.click(screen.getByRole('button', { name: 'View template' }));

    // Revealed template shows the real loader contract: a top-level register()
    // (what the backend validates with `ast`) and the executor signature.
    const code = await screen.findByText(/def register\(app, executor_registry\)/);
    expect(code.textContent).toContain('def my_tool(tool_input, transcript, attachments)');
    expect(screen.getByRole('button', { name: 'Copy' })).toBeInTheDocument();

    // The whole panel's rendered copy must be free of em dashes (repo style).
    expect(container.textContent).not.toContain('—');

    // Toggling again hides it.
    fireEvent.click(screen.getByRole('button', { name: 'Hide template' }));
    expect(screen.queryByText(/def register\(/)).not.toBeInTheDocument();
  });

  it('copies the template to the clipboard', async () => {
    api.get.mockResolvedValue(DISABLED_LIST);
    const writeText = vi.fn(async (_text: string) => undefined);
    Object.defineProperty(navigator, 'clipboard', {
      value: { writeText },
      configurable: true,
      writable: true,
    });

    renderPanel();
    await screen.findByText('e2e_plug');

    fireEvent.click(screen.getByRole('button', { name: 'View template' }));
    fireEvent.click(screen.getByRole('button', { name: 'Copy' }));

    await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
    const copied = writeText.mock.calls[0][0] as string;
    expect(copied).toContain('def register(app, executor_registry):');
    expect(copied).toContain("executor_registry[\"my_tool\"] = my_tool");
    expect(copied).not.toContain('—');
  });

  it('enables a plugin live and shows its tools after refetch', async () => {
    // First list load = disabled; after the toggle refetch = enabled w/ tools.
    api.get.mockResolvedValueOnce(DISABLED_LIST).mockResolvedValue(ENABLED_LIST);
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      status: 200,
      json: async () => ({
        name: 'e2e_plug',
        enabled: true,
        loaded: true,
        restartRequired: false,
        note: 'Enabled. Its tools are available on your next message.',
      }),
    }));
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    renderPanel();
    await screen.findByText('e2e_plug');

    // Flip the non-protected switch on.
    const boxes = screen.getAllByRole('checkbox') as HTMLInputElement[];
    const target = boxes.find((b) => !b.disabled)!;
    fireEvent.click(target);

    await waitFor(() =>
      expect(fetchMock).toHaveBeenCalledWith('/api/plugins/e2e_plug/toggle', { method: 'PATCH' }),
    );
    // After refetch the live-registered tool is surfaced.
    await waitFor(() => expect(screen.getByText(/Tools: e2e_ping/)).toBeInTheDocument());
  });
});
