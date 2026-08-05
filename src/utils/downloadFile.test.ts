import { afterEach, beforeEach, describe, expect, it, vi, type MockInstance } from 'vitest';
import { downloadFile, downloadUrl, REVOKE_DELAY_MS } from './downloadFile';

/**
 * The shared download helper is the single mechanism every export entry
 * point uses. The contract under test:
 *   - a temporary anchor is clicked with BOTH a blob href and the `download`
 *     attribute set to the target filename (the `download` attribute is what
 *     turns the click into a save in browsers and into
 *     `navigationAction.shouldPerformDownload` in the macOS shell WKWebView);
 *   - the anchor does not linger in the DOM;
 *   - the object URL is revoked, but only after a delay (an immediate revoke
 *     races the WKDownload's asynchronous blob fetch).
 */
describe('downloadFile', () => {
  const clicks: HTMLAnchorElement[] = [];
  let clickSpy: MockInstance<() => void>;
  let createSpy: MockInstance<(obj: Blob | MediaSource) => string>;
  let revokeSpy: MockInstance<(url: string) => void>;

  beforeEach(() => {
    vi.useFakeTimers();
    clicks.length = 0;
    // jsdom implements neither anchor download navigation nor blob URLs.
    clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, 'click')
      .mockImplementation(function (this: HTMLAnchorElement) {
        // Snapshot the anchor at click time (cloneNode keeps attributes but
        // not DOM position, so also record connectedness separately).
        clicks.push(this);
        expect(this.isConnected).toBe(true);
      });
    createSpy = vi
      .spyOn(URL, 'createObjectURL')
      .mockReturnValue('blob:mock-url-1');
    revokeSpy = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
  });

  afterEach(() => {
    vi.restoreAllMocks();
    vi.useRealTimers();
  });

  it('clicks a temporary anchor with the download attribute and blob href', () => {
    downloadFile('# hello', 'conversation-abc.md', 'text/markdown');

    expect(clickSpy).toHaveBeenCalledTimes(1);
    expect(clicks).toHaveLength(1);
    const a = clicks[0];
    expect(a.getAttribute('download')).toBe('conversation-abc.md');
    expect(a.getAttribute('href')).toBe('blob:mock-url-1');
    // Removed again after the click — no anchor debris in the DOM.
    expect(a.isConnected).toBe(false);
    expect(document.querySelectorAll('a[download]')).toHaveLength(0);
  });

  it('wraps string content in a Blob with the given MIME type', () => {
    downloadFile('plain text', 'transcript-x.txt', 'text/plain');
    const blob = createSpy.mock.calls[0][0] as Blob;
    expect(blob).toBeInstanceOf(Blob);
    expect(blob.type).toBe('text/plain');
  });

  it('passes a Blob through unchanged', () => {
    const blob = new Blob(['x'], { type: 'text/markdown' });
    downloadFile(blob, 'file.md');
    expect(createSpy).toHaveBeenCalledWith(blob);
  });

  it('revokes the object URL only after the delay', () => {
    downloadFile('content', 'file.txt', 'text/plain');
    // Not synchronously — WKWebView's WKDownload fetches the blob async.
    expect(revokeSpy).not.toHaveBeenCalled();
    vi.advanceTimersByTime(REVOKE_DELAY_MS - 1);
    expect(revokeSpy).not.toHaveBeenCalled();
    vi.advanceTimersByTime(1);
    expect(revokeSpy).toHaveBeenCalledWith('blob:mock-url-1');
  });
});

describe('downloadUrl', () => {
  afterEach(() => {
    vi.restoreAllMocks();
  });

  it('clicks an anchor pointing at the URL with the download attribute, no blob', () => {
    const clicks: HTMLAnchorElement[] = [];
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(function (
      this: HTMLAnchorElement,
    ) {
      clicks.push(this);
    });
    const createSpy = vi.spyOn(URL, 'createObjectURL');
    const revokeSpy = vi.spyOn(URL, 'revokeObjectURL');

    downloadUrl('/api/costs/export?format=csv', 'costs-export.csv');

    expect(clicks).toHaveLength(1);
    expect(clicks[0].getAttribute('href')).toBe('/api/costs/export?format=csv');
    expect(clicks[0].getAttribute('download')).toBe('costs-export.csv');
    expect(createSpy).not.toHaveBeenCalled();
    expect(revokeSpy).not.toHaveBeenCalled();
  });
});
