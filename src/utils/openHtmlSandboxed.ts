/**
 * Open model-authored HTML in a new tab WITHOUT granting it the app's origin.
 *
 * A `blob:` URL inherits the opener's origin, so `window.open(blobUrl)` would
 * run the HTML same-origin with the app (http://127.0.0.1:8000) and let any
 * script in it silently call the local backend API (write files, run shell,
 * change config). Model output is attacker-influenceable via prompt injection,
 * so that is a one-click local-API takeover.
 *
 * Instead we open a blank tab and mount the content inside a sandboxed iframe
 * (`allow-scripts`, deliberately NO `allow-same-origin` -> opaque origin), so
 * scripts in the artifact run isolated and cannot reach the app, its cookies,
 * or the backend. Mirrors the inline preview iframe used elsewhere.
 *
 * Inside the packaged macOS shell (macapp/shell/main.swift) this trick can't
 * escape to a real browser tab: `window.open('about:blank')` produces no
 * navigable URL, so the shell's http(s)-only external-link handling has
 * nothing to hand off to NSWorkspace and the popup just stays in the native
 * window. When the shell's `openExternalHtml` bridge is present, use that
 * instead — it writes the HTML to a throwaway file and opens it with the
 * user's actual default browser.
 */
export function openHtmlSandboxed(html: string): void {
  const nativeBridge = (
    window as unknown as {
      webkit?: { messageHandlers?: { openExternalHtml?: { postMessage: (html: string) => void } } };
    }
  ).webkit?.messageHandlers?.openExternalHtml;
  if (nativeBridge) {
    nativeBridge.postMessage(html);
    return;
  }

  const win = window.open('about:blank', '_blank');
  if (!win) return; // popup blocked
  const doc = win.document;
  doc.body.style.margin = '0';
  const iframe = doc.createElement('iframe');
  // Attribute form (not the DOMTokenList) so the sandbox is applied before load.
  iframe.setAttribute('sandbox', 'allow-scripts');
  // srcdoc as a property avoids any attribute-escaping of the HTML.
  iframe.srcdoc = html;
  iframe.style.cssText =
    'position:fixed;inset:0;width:100%;height:100%;border:none;background:#fff';
  doc.body.appendChild(iframe);
}
