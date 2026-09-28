import { beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, within } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import { CodeToolsStatusResponseSchema } from '@/types/schemas/codeTools.schema';

const api = vi.hoisted(() => ({ get: vi.fn(), put: vi.fn(), post: vi.fn(), del: vi.fn() }));
vi.mock('@/api/client', () => api);

import { CodeToolsPanel } from './CodeToolsPanel';
import { SettingsModal, SETTINGS_TABS, TAB_ROUTE } from './SettingsModal';
import { useUIStore } from '@/stores/uiStore';

/** The shape server/code_tools/status.py returns (tests/test_code_tools_status.py
 *  pins the backend side of it). */
const STATUS = {
  workspace: '/Users/me/proj',
  tools: [
    {
      id: 'ruff',
      name: 'Ruff',
      powers: 'Python checks for the assistant: lsp_diagnostics, and a check after every Python file it writes',
      ok: true,
      version: '0.14.2',
      source: 'Bundled with the app',
      command: '/App/python3 -m ruff',
      reason: '',
      note: 'This workspace configures ruff (pyproject.toml), so after each write ruff also fixes what it can and formats the file.',
    },
    {
      id: 'eslint',
      name: 'ESLint',
      powers: 'JS/TS checks for the assistant: lsp_diagnostics',
      ok: false,
      version: null,
      source: "The workspace's node_modules",
      command: '',
      reason: 'This workspace has no ESLint (no node_modules/eslint at its root).',
      note: '',
    },
  ],
};

function renderPanel() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <CodeToolsPanel />
    </QueryClientProvider>,
  );
}

beforeEach(() => {
  vi.clearAllMocks();
});

describe('CodeToolsPanel', () => {
  it('the fixture matches the declared response schema', () => {
    expect(CodeToolsStatusResponseSchema.safeParse(STATUS).success).toBe(true);
  });

  it('reads the code tools status endpoint with its schema', async () => {
    api.get.mockResolvedValue(STATUS);
    renderPanel();
    await screen.findByTestId('code-tool-ruff');
    const [url, options] = api.get.mock.calls[0];
    expect(url).toBe('/api/code-tools/status');
    expect(options.schema).toBe(CodeToolsStatusResponseSchema);
  });

  it('shows what each tool powers, its state, version and command', async () => {
    api.get.mockResolvedValue(STATUS);
    renderPanel();
    const ruff = await screen.findByTestId('code-tool-ruff');
    expect(within(ruff).getByText('Working')).toBeInTheDocument();
    expect(within(ruff).getByText('0.14.2')).toBeInTheDocument();
    expect(within(ruff).getByText(STATUS.tools[0].powers)).toBeInTheDocument();
    expect(within(ruff).getByText('/App/python3 -m ruff')).toBeInTheDocument();
    expect(within(ruff).getByRole('note')).toHaveTextContent('formats the file');
  });

  it('gives the reason for a tool that does not work', async () => {
    api.get.mockResolvedValue(STATUS);
    renderPanel();
    const eslint = await screen.findByTestId('code-tool-eslint');
    expect(within(eslint).getByText('Not available')).toBeInTheDocument();
    expect(within(eslint).getByRole('note')).toHaveTextContent('This workspace has no ESLint');
  });

  it('asks for a workspace when none is connected', async () => {
    api.get.mockResolvedValue({ ...STATUS, workspace: null });
    renderPanel();
    expect(await screen.findByText(/Connect a workspace to check its own tools/)).toBeInTheDocument();
  });
});

describe('Settings routing for Code tools', () => {
  it('is a destination under Tools and automation', () => {
    expect(SETTINGS_TABS.some((t) => t.id === 'code-tools' && t.label === 'Code tools')).toBe(true);
    expect(TAB_ROUTE['code-tools']).toEqual({ rail: 'code-tools', sub: 'code-tools' });
  });

  it('opens the Code tools page', async () => {
    api.get.mockResolvedValue(STATUS);
    useUIStore.setState({ settingsOpen: true, settingsTab: 'code-tools' });
    const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    render(
      <QueryClientProvider client={qc}>
        <SettingsModal />
      </QueryClientProvider>,
    );
    expect(screen.getByRole('button', { name: /Code tools/i })).toHaveAttribute('aria-current', 'page');
    expect(await screen.findByTestId('code-tool-ruff')).toBeInTheDocument();
  });
});
