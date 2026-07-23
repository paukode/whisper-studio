import { describe, it, expect, vi, afterEach } from 'vitest';
import { openHtmlSandboxed } from './openHtmlSandboxed';

afterEach(() => {
  vi.restoreAllMocks();
});

describe('openHtmlSandboxed', () => {
  it('mounts the HTML in a sandboxed iframe with no same-origin access', () => {
    const doc = document.implementation.createHTMLDocument('');
    const fakeWin = { document: doc } as unknown as Window;
    const openSpy = vi.spyOn(window, 'open').mockReturnValue(fakeWin);

    openHtmlSandboxed('<script>fetch("/api/config")</script>');

    expect(openSpy).toHaveBeenCalledWith('about:blank', '_blank');
    const iframe = doc.querySelector('iframe');
    expect(iframe).not.toBeNull();
    const sandbox = iframe!.getAttribute('sandbox') ?? '';
    // allow-scripts yes, allow-same-origin NEVER (that would defeat the isolation).
    expect(sandbox.split(/\s+/)).toContain('allow-scripts');
    expect(sandbox).not.toContain('allow-same-origin');
    // Content is carried via srcdoc, not a same-origin blob: URL.
    expect(iframe!.srcdoc).toContain('fetch("/api/config")');
    expect(iframe!.getAttribute('src')).toBeNull();
  });

  it('does nothing when the popup is blocked', () => {
    vi.spyOn(window, 'open').mockReturnValue(null);
    expect(() => openHtmlSandboxed('<p>hi</p>')).not.toThrow();
  });
});
