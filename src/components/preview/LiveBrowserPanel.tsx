import React, { useCallback, useEffect, useMemo, useState } from 'react';
import { useDockStore } from '@/stores/dockStore';
import { startPreviewSession, stopPreviewSession } from '@/api/preview';
import { LivePanel } from './LivePanel';

/**
 * LiveBrowserPanel — the live preview as a small browser. When the dev server
 * is running it shows an interactive iframe of the site (with back/forward/
 * refresh + a URL bar) or, toggled, the read-only screencast of the assistant's
 * browser. A Stop button stops the dev server; when stopped it shows a
 * "Preview server stopped" state with Restart. Back/forward track URL-bar
 * navigations only — the site is cross-origin, so in-page clicks can't be read.
 *
 * Readiness gate: a dev server is registered the instant it's *spawned*, but a
 * heavy backend (FastAPI/Django/Rails) doesn't accept connections for several
 * seconds after that. Mounting the iframe immediately would load Chromium's
 * cached ERR_CONNECTION_REFUSED page and never recover (an iframe doesn't retry,
 * and the target is cross-origin so we can't detect the failure to reload it).
 * So we probe the target first and only mount the iframe once it answers,
 * showing a "Waiting for the dev server…" state meanwhile. The probe keeps
 * retrying until the server is up (the auto-retry), so a slow boot self-heals.
 */

// Retry cadence while the server isn't answering yet, per-attempt abort budget,
// and how long to wait before nudging the user that the boot is unusually slow.
const PROBE_RETRY_MS = 800;
const PROBE_TIMEOUT_MS = 2500;
const SLOW_HINT_MS = 15000;

/**
 * Is `url` accepting connections? We can't read a cross-origin response, but a
 * *resolved* no-cors fetch means the socket accepted and the server replied —
 * enough to know the iframe won't hit ERR_CONNECTION_REFUSED. A refused/closed
 * socket rejects; a hung connect is bounded by the abort timeout (→ not ready).
 */
async function probeReachable(url: string, timeoutMs: number): Promise<boolean> {
  const ctrl = new AbortController();
  const timer = setTimeout(() => ctrl.abort(), timeoutMs);
  try {
    await fetch(url, { mode: 'no-cors', cache: 'no-store', signal: ctrl.signal });
    return true;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}

const iconBtn: React.CSSProperties = {
  display: 'inline-flex', alignItems: 'center', justifyContent: 'center',
  width: 26, height: 26, padding: 0, flex: '0 0 auto',
  background: 'transparent', border: '1px solid var(--border, #ddd)', borderRadius: 6,
  color: 'var(--text-secondary, #555)', cursor: 'pointer',
};
const txtBtn: React.CSSProperties = {
  font: 'inherit', fontSize: 12, cursor: 'pointer', borderRadius: 6, padding: '4px 10px',
  border: '1px solid var(--border-strong, #ccc)', background: 'transparent', color: 'var(--text-secondary, #555)', flex: '0 0 auto',
};

function I({ d }: { d: string }) {
  return (
    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
      <path d={d} />
    </svg>
  );
}

// Self-contained SMIL spinner — no global keyframes needed for an inline-styled
// component.
function Spinner() {
  return (
    <svg width="22" height="22" viewBox="0 0 24 24" fill="none" stroke="var(--text-muted, #888)" strokeWidth="2.4" strokeLinecap="round" aria-hidden="true">
      <path d="M12 3a9 9 0 1 0 9 9">
        <animateTransform attributeName="transform" type="rotate" from="0 12 12" to="360 12 12" dur="0.9s" repeatCount="indefinite" />
      </path>
    </svg>
  );
}

export const LiveBrowserPanel: React.FC<{ name: string; url?: string | null; port?: number | null }> = ({ name, url, port }) => {
  const liveSession = useDockStore((s) => s.liveSession);
  const liveNavUrl = useDockStore((s) => s.liveNavUrl);
  const running = liveSession != null && liveSession.name === name;

  // The pane's target: a routed localhost URL wins, else the running session,
  // else the props/port fallback. The component is remounted (RightDock keys it
  // by dockStore.liveNavKey) whenever this target changes, so local nav state
  // below always starts fresh from this value — no in-place merging needed.
  const siteUrl = useMemo(() => {
    const u = liveNavUrl || (running ? liveSession?.url : url) || url || liveSession?.url;
    if (u) return u;
    const p = (running ? liveSession?.port : port) || port || liveSession?.port;
    return p ? `http://localhost:${p}` : '';
  }, [running, liveSession, liveNavUrl, url, port]);

  const [mode, setMode] = useState<'interact' | 'watch'>('interact');
  const [navStack, setNavStack] = useState<string[] | null>(null);
  const [histIndex, setHistIndex] = useState(0);
  const [typed, setTyped] = useState<string | null>(null);
  const [refreshKey, setRefreshKey] = useState(0);
  const [busy, setBusy] = useState(false);

  const stack = navStack ?? (siteUrl ? [siteUrl] : []);
  const shownUrl = stack[histIndex] ?? siteUrl;
  const inputValue = typed ?? shownUrl;

  // Readiness gate. `token` identifies the exact target+refresh being probed;
  // each gate flag is stored as "the token it applies to" so it resets to its
  // default automatically when the target changes — no synchronous setState in
  // an effect (which cascades renders). `ready` unlocks the iframe, `slow`
  // shows the took-too-long hint, `forceShow` is the manual "Open anyway".
  const token = `${shownUrl}::${refreshKey}`;
  const [readyToken, setReadyToken] = useState<string | null>(null);
  const [slowToken, setSlowToken] = useState<string | null>(null);
  const [forceShowToken, setForceShowToken] = useState<string | null>(null);
  const [loadKey, setLoadKey] = useState(0);
  const ready = readyToken === token;
  const forceShow = forceShowToken === shownUrl;
  const slow = slowToken === token && !ready;

  const navigate = useCallback((raw: string) => {
    let u = raw.trim();
    if (!u) return;
    if (!/^https?:\/\//i.test(u)) u = 'http://' + u;
    setNavStack((prev) => {
      const base = prev ?? (siteUrl ? [siteUrl] : []);
      const trimmed = base.slice(0, histIndex + 1);
      return [...trimmed, u];
    });
    setHistIndex((i) => i + 1);
    setTyped(null);
  }, [siteUrl, histIndex]);

  const back = useCallback(() => { setHistIndex((i) => Math.max(0, i - 1)); setTyped(null); }, []);
  const forward = useCallback(() => { setHistIndex((i) => Math.min(stack.length - 1, i + 1)); setTyped(null); }, [stack.length]);
  const refresh = useCallback(() => setRefreshKey((k) => k + 1), []);

  const closeLive = useDockStore((s) => s.closeLive);
  const stop = useCallback(async () => { setBusy(true); try { await stopPreviewSession(name); } finally { setBusy(false); } }, [name]);
  const restart = useCallback(async () => { setBusy(true); try { await startPreviewSession(name); } finally { setBusy(false); } }, [name]);

  // Probe the target until it answers, then unlock the iframe. Re-runs (and so
  // re-gates, since `ready` keys off `token`) on a new target or a Refresh. The
  // Watch screencast streams the assistant's backend browser, which is always
  // reachable, so it's exempt. All setState happens in the async callback, not
  // the effect body.
  useEffect(() => {
    if (mode === 'watch' || !shownUrl) return;
    let cancelled = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    const tick = async () => {
      const ok = await probeReachable(shownUrl, PROBE_TIMEOUT_MS);
      if (cancelled) return;
      if (ok) {
        // Fresh key so the frame does a clean first load instead of any cached
        // refused-connection page from an earlier attempt.
        setLoadKey((k) => k + 1);
        setReadyToken(token);
        return; // server is up; the iframe owns the connection now
      }
      timer = setTimeout(tick, PROBE_RETRY_MS);
    };
    void tick();
    return () => { cancelled = true; if (timer) clearTimeout(timer); };
  }, [token, shownUrl, mode]);

  // Surface the slow-boot hint if the target still hasn't answered after a
  // while (keyed to `token`, so it clears on a new target).
  useEffect(() => {
    if (mode === 'watch' || !shownUrl) return;
    const t = setTimeout(() => setSlowToken(token), SLOW_HINT_MS);
    return () => clearTimeout(t);
  }, [token, shownUrl, mode]);

  // ── Stopped: no running server AND no routed URL to show. A URL-only view (a
  //    localhost link routed in without a registered session) skips this. ──
  if (!running && !liveNavUrl) {
    return (
      <div style={{ flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12, padding: 16, background: 'var(--bg-inset, #0d0d0d)' }}>
        <div style={{ color: 'var(--text-muted, #888)', fontSize: 13 }}>Preview server stopped</div>
        <div style={{ display: 'flex', gap: 8 }}>
          <button type="button" style={txtBtn} onClick={() => void restart()} disabled={busy}>{busy ? 'Starting…' : 'Restart'}</button>
          <button type="button" style={txtBtn} onClick={closeLive}>Close</button>
        </div>
      </div>
    );
  }

  const showIframe = ready || forceShow;

  return (
    <div style={{ flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'column' }}>
      {/* Browser chrome */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 6, padding: '6px 8px', borderBottom: '1px solid var(--border, #333)', background: 'var(--bg-secondary, #222)', flex: '0 0 auto' }}>
        <button type="button" style={{ ...iconBtn, opacity: histIndex > 0 ? 1 : 0.4 }} onClick={back} disabled={histIndex <= 0} title="Back" aria-label="Back"><I d="M15 18l-6-6 6-6" /></button>
        <button type="button" style={{ ...iconBtn, opacity: histIndex < stack.length - 1 ? 1 : 0.4 }} onClick={forward} disabled={histIndex >= stack.length - 1} title="Forward" aria-label="Forward"><I d="M9 18l6-6-6-6" /></button>
        <button type="button" style={iconBtn} onClick={refresh} title="Refresh" aria-label="Refresh"><I d="M23 4v6h-6M1 20v-6h6M3.5 9a9 9 0 0 1 14.9-3.4L23 10M1 14l4.6 4.4A9 9 0 0 0 20.5 15" /></button>
        <input
          value={inputValue}
          onChange={(e) => setTyped(e.target.value)}
          onKeyDown={(e) => { if (e.key === 'Enter') navigate(inputValue); }}
          spellCheck={false}
          aria-label="Preview URL"
          style={{ flex: 1, minWidth: 0, font: 'inherit', fontSize: 12, padding: '4px 8px', borderRadius: 6, border: '1px solid var(--border, #ddd)', background: 'var(--bg-primary, #1a1a1a)', color: 'var(--text-primary, #eee)' }}
        />
        {running && (
          <button type="button" style={{ ...txtBtn, ...(mode === 'watch' ? { background: 'var(--bg-accent)', color: 'var(--text-accent)' } : {}) }} onClick={() => setMode(mode === 'interact' ? 'watch' : 'interact')} title={mode === 'interact' ? 'Watch the assistant drive (read-only)' : 'Interact with the site'}>
            {mode === 'interact' ? 'Watch' : 'Interact'}
          </button>
        )}
        {running && (
          <button type="button" style={txtBtn} onClick={() => void stop()} disabled={busy} title="Stop the dev server">Stop</button>
        )}
      </div>

      {/* Body: the Watch screencast is only available for a running registered
          session; a URL-only view always uses the interactive iframe, which is
          gated on readiness (below). */}
      {mode === 'watch' && running ? (
        <LivePanel name={name} />
      ) : showIframe ? (
        <iframe
          key={`${shownUrl}::${loadKey}`}
          src={shownUrl}
          title="Live preview"
          style={{ flex: '1 1 auto', width: '100%', border: 'none', background: '#fff', minHeight: 0 }}
        />
      ) : (
        <div style={{ flex: '1 1 auto', minHeight: 0, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', gap: 12, padding: 16, background: 'var(--bg-inset, #0d0d0d)', textAlign: 'center' }}>
          <Spinner />
          <div style={{ color: 'var(--text-secondary, #aaa)', fontSize: 13 }}>Waiting for the dev server…</div>
          <div style={{ color: 'var(--text-muted, #777)', fontSize: 11, fontFamily: 'var(--font-mono, ui-monospace, monospace)', wordBreak: 'break-all', maxWidth: 340 }}>{shownUrl}</div>
          {slow && (
            <>
              <div style={{ color: 'var(--text-muted, #777)', fontSize: 11, maxWidth: 340, lineHeight: 1.5 }}>
                Taking longer than usual — the server may still be booting or failed to start. Check the terminal or preview logs.
              </div>
              <div style={{ display: 'flex', gap: 8 }}>
                <button type="button" style={txtBtn} onClick={refresh}>Retry</button>
                <button type="button" style={txtBtn} onClick={() => setForceShowToken(shownUrl)}>Open anyway</button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
};

export default LiveBrowserPanel;
