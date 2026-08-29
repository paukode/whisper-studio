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
// Documentation references from @docs answers: `#docspage=<url-encoded page>`
// with an optional `&h=<heading id>`. Opens the bundled docs site in the dock.
const DOCS_PREFIX = '#docspage=';
// Pages are flat filenames inside docs/; anything else (path separators,
// traversal) is rejected and the click falls through as a no-op link.
const DOCS_PAGE_RE = /^[\w][\w.-]*\.html$/;

export interface DocsPageTarget {
  page: string;
  anchor?: string;
}

/** Parse a `#docspage=<url-encoded page>&h=<anchor>` reference href.
 * Returns null for non-docs hrefs or an invalid page name. */
export function parseDocsPageHref(href: string): DocsPageTarget | null {
  if (!href.startsWith(DOCS_PREFIX)) return null;
  const frag = href.slice(DOCS_PREFIX.length);
  const amp = frag.indexOf('&');
  const rawPage = amp === -1 ? frag : frag.slice(0, amp);
  let page: string;
  try {
    page = decodeURIComponent(rawPage);
  } catch {
    page = rawPage;
  }
  if (!DOCS_PAGE_RE.test(page)) return null;
  const t: DocsPageTarget = { page };
  if (amp !== -1) {
    for (const param of frag.slice(amp + 1).split('&')) {
      if (param.startsWith('h=')) {
        try {
          t.anchor = decodeURIComponent(param.slice(2));
        } catch {
          t.anchor = param.slice(2);
        }
      }
    }
  }
  return t;
}
// Matches http(s) URLs whose host is loopback, so a dev-server link routes to
// the Live pane rather than hijacking the app tab.
const LOCALHOST_RE = /^https?:\/\/(localhost|127\.0\.0\.1|0\.0\.0\.0|\[::1\])(:\d+)?([/?#]|$)/i;

export interface WsFileTarget {
  path: string;
  startLine?: number;
  endLine?: number;
  /** `os` when the link explicitly asks to open with the OS default app
   * (server.index.citations.created_file_link — a just-written deliverable,
   * not a citation into an existing file) rather than the in-app dock. */
  openMode?: 'os';
}

/**
 * Parse a `#wsfile=<url-quoted path>&L=<start>-<end>` citation href, or a
 * `#wsfile=<url-quoted path>&open=os` created-file href.
 *
 * The path is url-quoted, so any literal `&`/`:` in it is percent-encoded and the
 * FIRST raw `&` is unambiguously the path/params boundary — we split there BEFORE
 * decoding (decoding the whole slice would corrupt a path containing `%26`).
 * `&L=`/`&open=` are optional and independent (legacy citation links omit both);
 * malformed/reversed ranges degrade gracefully. Returns null for non-`#wsfile=` hrefs.
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
      const lineMatch = /^L=(\d+)(?:-(\d+))?$/.exec(param);
      if (lineMatch) {
        let s = parseInt(lineMatch[1], 10);
        let e = lineMatch[2] ? parseInt(lineMatch[2], 10) : s;
        if (s < 1) continue;
        if (e < s) [s, e] = [e, s];
        t.startLine = s;
        t.endLine = e;
        continue;
      }
      if (param === 'open=os') {
        t.openMode = 'os';
        continue;
      }
      // unknown/malformed params are ignored (forward-compatible)
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

// A just-created deliverable: open with the OS default app, the way a Finder
// double-click would, instead of the in-app dock viewer. /open-with resolves
// an absolute path fine (os.path.join discards the workspace root when the
// second argument is already absolute) so no relative-path juggling is needed.
async function openFileExternally(t: WsFileTarget, name: string): Promise<void> {
  try {
    await post('/api/workspace/open-with', { path: t.path });
  } catch {
    useUIStore.getState().addToast({
      type: 'error',
      message: `Couldn't open ${name} — is its workspace still connected?`,
    });
  }
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

    // Documentation references open the docs pane at the cited page/heading,
    // the same pane the header's ? button opens.
    const docsTarget = parseDocsPageHref(href);
    if (docsTarget) {
      e.preventDefault();
      useDockStore.getState().openDocs(docsTarget.page, docsTarget.anchor);
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

    // A created-file link has no in-app dock view to fall back to, so a
    // plain click opens it externally. Modifier-click is handled above and
    // still means "reveal in Finder" either way, for consistency with every
    // other #wsfile= link.
    if (target.openMode === 'os') {
      void openFileExternally(target, name);
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
    if (href.startsWith(DOCS_PREFIX)) {
      anchor.title = 'Opens this documentation page in the side panel';
      return;
    }
    if (!href.startsWith(PREFIX)) return;
    // navigator.platform is deprecated; prefer userAgentData where available.
    const platform = ((navigator as { userAgentData?: { platform?: string } }).userAgentData?.platform ?? navigator.platform ?? '').toLowerCase();
    const isMac = platform.includes('mac');
    // Non-mac says "file manager": Windows reveals in Explorer, but Linux only
    // opens the containing folder, so don't promise a specific app there.
    const revealHint = isMac
      ? 'Cmd-click to reveal in Finder.'
      : 'Ctrl-click to reveal in your file manager.';
    anchor.title = href.includes('&open=os')
      ? `Click to open in your default app. ${revealHint}`
      : `Click to open in the side panel. ${revealHint}`;
  };

  container.addEventListener('click', onClick);
  container.addEventListener('mouseover', onOver);
  return () => {
    container.removeEventListener('click', onClick);
    container.removeEventListener('mouseover', onOver);
  };
}
