import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { ImportSkillsDialog } from './ImportSkillsDialog';
import { post } from '@/api/client';

vi.mock('@/api/client', () => ({
  post: vi.fn(() => Promise.resolve({ skills: [] })),
}));

function renderDialog(props: Partial<React.ComponentProps<typeof ImportSkillsDialog>> = {}) {
  const onClose = vi.fn();
  const onImported = vi.fn();
  render(<ImportSkillsDialog onClose={onClose} onImported={onImported} {...props} />);
  return { onClose, onImported };
}

describe('ImportSkillsDialog: backdrop dismissal', () => {
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

describe('ImportSkillsDialog: local folder mode', () => {
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

  it('names a skill that was not imported because one with the same name exists', async () => {
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
      return {
        ok: true,
        json: async () => ({
          imported: [],
          conflicts: [{ subpath: 'pack/alpha', name: 'alpha', reason: 'a skill named alpha already exists' }],
          errors: [],
        }),
      };
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { onClose } = renderDialog();
    fireEvent.click(screen.getByRole('tab', { name: 'From local folder' }));
    const input = screen.getByLabelText('Choose skills folder') as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [fileWithRelPath('pack/alpha/SKILL.md')],
      configurable: true,
    });
    fireEvent.change(input);
    await waitFor(() => expect(screen.getByText('pack/alpha')).toBeInTheDocument());

    // Overwrite existing is there, and off by default.
    const overwriteBox = screen.getByLabelText('Overwrite existing') as HTMLInputElement;
    expect(overwriteBox.checked).toBe(false);
    const row = screen.getByText('pack/alpha').closest('label') as HTMLElement;
    fireEvent.click(row.querySelector('input[type="checkbox"]') as HTMLElement);
    fireEvent.click(screen.getByRole('button', { name: /Import/ }));

    await waitFor(() =>
      expect(screen.getByText(/Not imported, a skill with the same name exists: alpha/)).toBeInTheDocument(),
    );
    const body = (fetchMock.mock.calls[1][1] as unknown as { body: FormData }).body;
    expect(body.get('overwrite')).toBe('false');
    expect(onClose).not.toHaveBeenCalled();
  });

  it('sends overwrite when ticked and names the reason of a skill it could not replace', async () => {
    const fetchMock = vi.fn(async (u: string, _init?: RequestInit) => {
      if (u === '/api/skills/import/local/preview') {
        return {
          ok: true,
          json: async () => ({
            skills: [
              { subpath: 'pack/alpha', name: 'alpha', hasScripts: false, scriptFiles: [], fileCount: 1 },
              { subpath: 'pack/meeting-notes', name: 'meeting_notes', hasScripts: false, scriptFiles: [], fileCount: 1 },
            ],
          }),
        };
      }
      return {
        ok: true,
        json: async () => ({
          imported: [{ name: 'alpha' }],
          conflicts: [{
            subpath: 'pack/meeting-notes',
            name: 'meeting_notes',
            reason: 'meeting_notes is a skill that comes with the app, import does not replace it',
          }],
          errors: [],
        }),
      };
    });
    vi.stubGlobal('fetch', fetchMock as unknown as typeof fetch);

    const { onClose } = renderDialog();
    fireEvent.click(screen.getByRole('tab', { name: 'From local folder' }));
    const input = screen.getByLabelText('Choose skills folder') as HTMLInputElement;
    Object.defineProperty(input, 'files', {
      value: [fileWithRelPath('pack/alpha/SKILL.md'), fileWithRelPath('pack/meeting-notes/SKILL.md')],
      configurable: true,
    });
    fireEvent.change(input);
    await waitFor(() => expect(screen.getByText('pack/alpha')).toBeInTheDocument());

    for (const sub of ['pack/alpha', 'pack/meeting-notes']) {
      const row = screen.getByText(sub).closest('label') as HTMLElement;
      fireEvent.click(row.querySelector('input[type="checkbox"]') as HTMLElement);
    }
    fireEvent.click(screen.getByLabelText('Overwrite existing'));
    fireEvent.click(screen.getByRole('button', { name: /Import/ }));

    await waitFor(() => expect(screen.getByText(/comes with the app/)).toBeInTheDocument());
    expect(screen.getByText(/Not imported: meeting_notes/)).toBeInTheDocument();
    const body = (fetchMock.mock.calls[1][1] as unknown as { body: FormData }).body;
    expect(body.get('overwrite')).toBe('true');
    expect(onClose).not.toHaveBeenCalled();
  });
});

describe('ImportSkillsDialog: git mode', () => {
  beforeEach(() => vi.clearAllMocks());

  it('sends the overwrite choice in the JSON body', async () => {
    const postMock = vi.mocked(post);
    postMock.mockImplementation(async (url: string) => {
      if (url === '/api/skills/import/preview') {
        return {
          skills: [{ subpath: 'skills/alpha', name: 'alpha', hasScripts: false, scriptFiles: [], fileCount: 1 }],
        };
      }
      return { imported: [{ name: 'alpha' }], conflicts: [], errors: [] };
    });

    const { onClose } = renderDialog();
    fireEvent.change(screen.getByPlaceholderText('https://github.com/owner/repo'), {
      target: { value: 'https://github.com/o/r' },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Preview' }));
    await waitFor(() => expect(screen.getByText('skills/alpha')).toBeInTheDocument());

    const row = screen.getByText('skills/alpha').closest('label') as HTMLElement;
    fireEvent.click(row.querySelector('input[type="checkbox"]') as HTMLElement);
    fireEvent.click(screen.getByLabelText('Overwrite existing'));
    fireEvent.click(screen.getByRole('button', { name: /Import/ }));

    await waitFor(() => expect(onClose).toHaveBeenCalled());
    expect(postMock).toHaveBeenCalledWith('/api/skills/import', {
      url: 'https://github.com/o/r',
      subpaths: ['skills/alpha'],
      overwrite: true,
    });
  });
});
