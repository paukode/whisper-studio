import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ImportSkillsDialog } from './ImportSkillsDialog';

vi.mock('@/api/client', () => ({
  post: vi.fn(() => Promise.resolve({ skills: [] })),
}));

function renderDialog(props: Partial<React.ComponentProps<typeof ImportSkillsDialog>> = {}) {
  const onClose = vi.fn();
  const onImported = vi.fn();
  render(<ImportSkillsDialog onClose={onClose} onImported={onImported} {...props} />);
  return { onClose, onImported };
}

describe('ImportSkillsDialog — backdrop dismissal', () => {
  beforeEach(() => vi.clearAllMocks());

  it('closes on a genuine backdrop press (mousedown + click on the overlay)', () => {
    const { onClose } = renderDialog();
    const overlay = screen.getByRole('dialog');

    fireEvent.mouseDown(overlay);
    fireEvent.click(overlay);

    expect(onClose).toHaveBeenCalledTimes(1);
  });

  it('does NOT close when a selection starts inside the content and releases on the backdrop', () => {
    const { onClose } = renderDialog();
    const overlay = screen.getByRole('dialog');
    const title = screen.getByText('Import skills');

    // Press begins on the content; the resulting click targets the overlay.
    fireEvent.mouseDown(title);
    fireEvent.click(overlay);

    expect(onClose).not.toHaveBeenCalled();
  });

  it('closes on the × button', () => {
    const { onClose } = renderDialog();
    fireEvent.click(screen.getByRole('button', { name: 'Close' }));
    expect(onClose).toHaveBeenCalledTimes(1);
  });
});

/** Build a File that carries a webkitRelativePath (jsdom does not populate it
 *  from the folder picker, so we define it explicitly). */
function fileWithRelPath(relPath: string, content = 'x'): File {
  const name = relPath.split('/').pop() ?? relPath;
  const f = new File([content], name, { type: 'text/markdown' });
  Object.defineProperty(f, 'webkitRelativePath', { value: relPath });
  return f;
}

describe('ImportSkillsDialog — local folder mode', () => {
  beforeEach(() => vi.clearAllMocks());

  it('scans a picked folder and lists detected skills without git', async () => {
    const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => ({
      ok: true,
      json: async () => ({
        skills: [
          { subpath: 'pack/alpha', name: 'alpha', description: 'The alpha skill', hasScripts: true, scriptFiles: ['run.py'], fileCount: 2 },
        ],
      }),
    }));
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    renderDialog();
    // Switch to the local-folder tab.
    fireEvent.click(screen.getByRole('tab', { name: 'From local folder' }));

    // Simulate the folder picker firing with a small skills tree.
    const input = screen.getByLabelText('Choose skills folder') as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [fileWithRelPath('pack/alpha/SKILL.md'), fileWithRelPath('pack/alpha/scripts/run.py')],
      configurable: true,
    });
    fireEvent.change(input);

    // The detected skill shows up (via the local preview endpoint, not git).
    await waitFor(() => expect(screen.getByText('pack/alpha')).toBeInTheDocument());
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/skills/import/local/preview',
      expect.objectContaining({ method: 'POST' }),
    );
    // The multipart body is a FormData carrying the relative-path parts.
    const body = (fetchMock.mock.calls[0][1] as unknown as { body: FormData }).body;
    expect(body).toBeInstanceOf(FormData);
    expect(body.getAll('files').length).toBe(2);
    expect(screen.getByText('The alpha skill')).toBeInTheDocument();
  });

  it('imports the selected local skills via the local endpoint', async () => {
    const fetchMock = vi.fn(async (u: string, _init?: RequestInit) => {
      if (u === '/api/skills/import/local/preview') {
        return {
          ok: true,
          json: async () => ({
            skills: [
              { subpath: 'pack/alpha', name: 'alpha', hasScripts: false, scriptFiles: [], fileCount: 1 },
            ],
          }),
        };
      }
      // import
      return { ok: true, json: async () => ({ imported: [{ name: 'alpha' }], conflicts: [], errors: [] }) };
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { onImported, onClose } = renderDialog();
    fireEvent.click(screen.getByRole('tab', { name: 'From local folder' }));

    const input = screen.getByLabelText('Choose skills folder') as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [fileWithRelPath('pack/alpha/SKILL.md')],
      configurable: true,
    });
    fireEvent.change(input);
    await waitFor(() => expect(screen.getByText('pack/alpha')).toBeInTheDocument());

    // Select it (the checkbox inside the skill row), then import.
    const row = screen.getByText('pack/alpha').closest('label') as HTMLElement;
    fireEvent.click(row.querySelector('input[type="checkbox"]') as HTMLElement);
    fireEvent.click(screen.getByRole('button', { name: /Import/ }));

    await waitFor(() => expect(onImported).toHaveBeenCalled());
    expect(fetchMock).toHaveBeenCalledWith(
      '/api/skills/import/local',
      expect.objectContaining({ method: 'POST' }),
    );
    // Clean success closes the dialog.
    expect(onClose).toHaveBeenCalled();
  });
});
