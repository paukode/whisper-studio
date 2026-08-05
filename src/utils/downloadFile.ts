/**
 * Shared file-download helper — the ONE mechanism every export entry point
 * uses (chat export, transcript export, sidebar session export, message
 * export, costs export, HTML artifact downloads, /export slash command).
 *
 * Mechanism: create a blob: URL and click a temporary anchor that carries the
 * `download` attribute with a concrete filename. This is correct in real
 * browsers (forces a save instead of a navigation) AND is what makes WebKit
 * flag the navigation action `shouldPerformDownload` in the macOS shell's
 * WKWebView — main.swift routes such navigations into a WKDownload
 * (macapp/shell/DownloadHandler.swift) instead of letting the web view
 * navigate to the raw blob and replace the app UI with it.
 *
 * The object URL is revoked on a timer, not synchronously: WKWebView starts
 * the WKDownload asynchronously after the click, and a synchronous revoke can
 * race the download's fetch of the blob. One minute is comfortably past any
 * realistic local blob fetch while still reclaiming the memory.
 */

export const REVOKE_DELAY_MS = 60_000;

/**
 * Download in-memory content as a file.
 *
 * @param content  Blob, or string content (wrapped in a Blob of `mimeType`).
 * @param filename Suggested filename, including extension.
 * @param mimeType MIME type used when `content` is a string. Ignored for Blobs.
 */
export function downloadFile(
  content: Blob | string,
  filename: string,
  mimeType = 'application/octet-stream',
): void {
  const blob = typeof content === 'string' ? new Blob([content], { type: mimeType }) : content;
  const url = URL.createObjectURL(blob);
  clickDownloadAnchor(url, filename);
  window.setTimeout(() => URL.revokeObjectURL(url), REVOKE_DELAY_MS);
}

/**
 * Download from a same-origin URL (e.g. an API endpoint that streams a file,
 * like /api/costs/export). No blob is created, so nothing to revoke.
 */
export function downloadUrl(url: string, filename: string): void {
  clickDownloadAnchor(url, filename);
}

function clickDownloadAnchor(href: string, filename: string): void {
  const a = document.createElement('a');
  a.href = href;
  a.download = filename;
  a.rel = 'noopener';
  // Appended to the DOM for the click: some engines ignore clicks on
  // unrooted anchors.
  document.body.appendChild(a);
  a.click();
  a.remove();
}
