import { get, post } from '@/api/client';
import { useUIStore } from '@/stores/uiStore';
import { useDockStore } from '@/stores/dockStore';

/**
 * Click delegation for chat links.
 *
 * Two kinds are intercepted so they land in the right-side dock instead of
 * navigating away or opening a browser tab:
 *  - Index / semantic-search "source" links use the `#wsfile=<url-encoded path>`
 *    fragment scheme (chosen because the fragment survives HTML sanitization).
 *    A plain click opens the file in the dock; a modifier-click (Cmd/Ctrl/Alt)
 *    reveals it in Finder/Explorer.
 *  - `http://localhost:*` / `127.0.0.1` / `[::1]` links (a dev server the
 *    assistant mentioned) open in the Live preview pane, mirroring Claude Code.
 * Returns a cleanup function.
 */
const PREFIX = '#wsfile=';
// Matches http(s) URLs whose host is loopback, so a dev-server link routes to
// the Live pane rather than hijacking the app tab.
const LOCALHOST_RE = /^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?([/?#]|$)/i;

export interface WsFileTarget {
  path: string;
  startLine?: number;
  endLine?: number;
}

/**
 * Parse a `#wsfile=<url-quoted path>&L=<start>-<end>` citation href.
 *
 * The path is url-quoted, so any literal `&`/`:` in it is percent-encoded and the
 * FIRST raw `&` is unambiguously the path/params boundary — we split there BEFORE
 * decoding (decoding the whole slice would corrupt a path containing `%26`).
 * `&L=` is optional (legacy links omit it); malformed/reversed ranges degrade
 * gracefully. Returns null for non-`#wsfile=` hrefs.
 */
export function parseWsFileHref(href: string): WsFileTarget | null {
  if (!href.startsWith(PREFIX)) return null;
  const frag = href.slice(PREFIX.length);
  const amp = frag.indexOf('&');
  const rawPath = amp === -1 ? frag : frag.slice(0, amp);
  let path: string;
  try {
    path = decodeURIComponent(rawPath);
  } catch {
    path = rawPath;
  }
  const t: WsFileTarget = { path };
  if (amp !== -1) {
    for (const param of frag.slice(amp + 1).split('&')) {
      const m = /^L=(\d+)(?:-(\d+))?$/.exec(param);
      if (!m) continue; // unknown/malformed params are ignored (forward-compatible)
      let s = parseInt(m[1], 10);
      let e = m[2] ? parseInt(m[2], 10) : s;
      if (s < 1) continue;
      if (e < s) [s, e] = [e, s];
      t.startLine = s;
      t.endLine = e;
    }
  }
  return t;
}

// The connected-workspace root, used to canonicalize the relative paths that
// legacy citations (old transcripts, pre-absolute-href) carry so they dedupe to
// the same dock panel as absolute ones. Fetched fresh per click (never cached):
// new citations are absolute so this runs only for rare legacy relative links,
// and a cache would go stale/negative when the user switches or disconnects a
// workspace — opening the wrong file.
async function workspaceRoot(): Promise<string | null> {
  try {
    const s = await get<{ connected: boolean; path?: string }>('/api/workspace/status');
    return s.connected && s.path ? s.path.replace(/\/+$/, '') : null;
  } catch {
    return null;
  }
}

async function openCitedFile(t: WsFileTarget): Promise<void> {
  let path = t.path;
  if (!path.startsWith('/')) {
    const root = await workspaceRoot();
    if (root) path = `${root}/${path}`;
  }
  useDockStore.getState().openFile({
    path,
    title: t.path.split('/').pop() || t.path,
    startLine: t.startLine,
    endLine: t.endLine,
  });
}

export function attachWsFileHandlers(container: HTMLElement): () => void {
  const onClick = (e: MouseEvent) => {
    const anchor = (e.target as HTMLElement | null)?.closest('a');
    if (!anchor) return;
    const href = anchor.getAttribute('href') || '';

    // Route localhost/dev-server links into the Live preview pane. A
    // modifier-click still falls through to the browser's default (open in a
    // real tab) as an escape hatch.
    if (LOCALHOST_RE.test(href) && !(e.metaKey || e.ctrlKey || e.altKey)) {
      e.preventDefault();
      useDockStore.getState().previewUrl(href);
      return;
    }

    const target = parseWsFileHref(href);
    if (!target) return;
    e.preventDefault();
    const name = target.path.split('/').pop() || target.path;

    // Escape hatch: modifier-click reveals the file in the OS file browser.
    if (e.metaKey || e.ctrlKey || e.altKey) {
      void post('/api/workspace/reveal', { path: target.path }).catch(() => {
        useUIStore.getState().addToast({
          type: 'error',
          message: `Couldn't reveal ${name} — is it in the connected workspace?`,
        });
      });
      return;
    }

    // Default: open the file in the dock at the cited lines (dedupes by path).
    void openCitedFile(target);
  };

  // Lazily add a discoverability tooltip on hover. Delegated (like the click
  // handler) so it survives streaming innerHTML swaps; setting `title` during
  // mouseover is early enough for the browser's hover-delay tooltip.
  const onOver = (e: MouseEvent) => {
    const anchor = (e.target as HTMLElement | null)?.closest('a');
    if (!anchor || anchor.title) return;
    const href = anchor.getAttribute('href') || '';
    if (LOCALHOST_RE.test(href)) {
      anchor.title = 'Opens in the Live preview pane';
      return;
    }
    if (!href.startsWith(PREFIX)) return;
    // navigator.platform is deprecated; prefer userAgentData where available.
    const platform = ((navigator as { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? '').toLowerCase();
    const isMac = platform.includes('mac');
    // Non-mac says "file manager": Windows reveals in Explorer, but Linux only
    // opens the containing folder, so don't promise a specific app there.
    anchor.title = isMac
      ? 'Click to open in the side panel. Cmd-click to reveal in Finder.'
      : 'Click to open in the side panel. Ctrl-click to reveal in your file manager.';
  };

  container.addEventListener('click', onClick);
  container.addEventListener('mouseover', onOver);
  return () => {
    container.removeEventListener('click', onClick);
    container.removeEventListener('mouseover', onOver);
  };
}
