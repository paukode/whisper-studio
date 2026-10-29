/**
 * The HTML page card shows, opens and saves the page in the app theme, and
 * a mode or palette switch reloads an open preview in the new colors.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';

const downloadFile = vi.fn();
const openHtmlSandboxed = vi.fn();
vi.mock('@/utils/downloadFile', () => ({ downloadFile: (...a: unknown[]) => downloadFile(...a) }));
vi.mock('@/utils/openHtmlSandboxed', () => ({
  openHtmlSandboxed: (...a: unknown[]) => openHtmlSandboxed(...a),
}));

import { ProgramArtifactCard } from './ProgramArtifactCard';

const PAGE = '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>hi</body></html>';
const root = document.documentElement;

function card() {
  return render(<ProgramArtifactCard title="Todo App" html={PAGE} description="" />);
}

function preview(container: HTMLElement): HTMLIFrameElement | null {
  return container.querySelector('iframe');
}

describe('ProgramArtifactCard', () => {
  beforeEach(() => {
    root.style.setProperty('--accent', '#9a82ff');
  });

  afterEach(() => {
    root.removeAttribute('style');
    root.removeAttribute('data-palette');
    downloadFile.mockReset();
    openHtmlSandboxed.mockReset();
  });

  it('previews the page with the theme block and the page itself', async () => {
    const { container } = card();
    fireEvent.click(screen.getByText('Preview'));
    await waitFor(() => expect(preview(container)).not.toBeNull());
    const doc = preview(container)!.getAttribute('srcdoc') ?? '';
    expect(doc).toContain('<style id="ws-theme">');
    expect(doc).toContain('--ws-accent:#9a82ff');
    expect(doc).toContain('<body>hi</body>');
  });

  it('reloads an open preview when the palette changes', async () => {
    const { container } = card();
    fireEvent.click(screen.getByText('Preview'));
    await waitFor(() => expect(preview(container)).not.toBeNull());
    act(() => {
      root.style.setProperty('--accent', '#166e8c');
      root.setAttribute('data-palette', 'harbor');
    });
    await waitFor(() =>
      expect(preview(container)!.getAttribute('srcdoc')).toContain('--ws-accent:#166e8c'),
    );
  });

  it('downloads and opens the page with the colors on screen', async () => {
    card();
    fireEvent.click(screen.getByText('Download'));
    await waitFor(() => expect(downloadFile).toHaveBeenCalledTimes(1));
    const [saved, name] = downloadFile.mock.calls[0];
    expect(name).toBe('todo-app.html');
    expect(saved).toContain('--ws-accent:#9a82ff');
    expect(String(saved).replace(/<style id="ws-theme">.*?<\/style>/, '')).toBe(PAGE);

    fireEvent.click(screen.getByText('Open'));
    await waitFor(() => expect(openHtmlSandboxed).toHaveBeenCalledTimes(1));
    expect(openHtmlSandboxed.mock.calls[0][0]).toContain('--ws-accent:#9a82ff');
  });
});
