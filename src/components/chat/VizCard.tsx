import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import DOMPurify from 'dompurify';
import type { VizArtifact } from '@/types/chat';
import { downloadFile } from '@/utils/downloadFile';
import {
  copyPngToClipboard,
  dataUrlToBlob,
  serializeSvg,
  slugify,
  standaloneSvg,
  svgSize,
  svgToPngBlob,
} from '@/utils/vizExport';


/** Theme colours handed to the chart host, which has no access to the app's
 *  stylesheet (opaque origin). Read live so charts follow theme switches. */
interface ThemeTokens {
  bg: string;
  textPrimary: string;
  textSecondary: string;
  textMuted: string;
  grid: string;
  danger: string;
  category: string[];
  ramp: string[];
}

function readThemeTokens(): ThemeTokens {
  const s = getComputedStyle(document.documentElement);
  const v = (name: string, fallback: string) => s.getPropertyValue(name).trim() || fallback;
  return {
    bg: v('--bg-surface', '#ffffff'),
    textPrimary: v('--text-primary', '#1f1e1d'),
    textSecondary: v('--text-secondary', '#6b6862'),
    textMuted: v('--text-muted', '#a3a097'),
    grid: v('--border', 'rgba(0,0,0,0.08)'),
    danger: v('--accent-record', '#d33b30'),
    category: [1, 2, 3, 4, 5, 6].map((i) => v(`--speaker-${i}`, '#888888')),
    // Sequential ramp runs from the page surface to the accent, so it reads
    // correctly light-on-dark and dark-on-light without a second palette.
    ramp: [v('--bg-elevated', '#f0eee6'), v('--accent', '#1d6ec2')],
  };
}

/** Watch `data-theme` on <html> and re-read tokens when it changes. */
function useThemeTokens(): ThemeTokens {
  const [tokens, setTokens] = useState(readThemeTokens);
  useEffect(() => {
    const observer = new MutationObserver(() => setTokens(readThemeTokens()));
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme'],
    });
    return () => observer.disconnect();
  }, []);
  return tokens;
}

type ExportFormat = 'svg' | 'png';

/* The chart host is delivered as an iframe `srcdoc` with the Vega runtime
 * inlined into it, assembled once per app session and shared by every chart.
 *
 * Why not `src` plus <script src>: the frame is sandboxed without
 * `allow-same-origin`, so it runs on an opaque origin. Both the document
 * navigation and its subresource loads are then at the mercy of whichever web
 * view is hosting the app, and we saw both refused. Inlining makes the frame
 * self-contained, which is also what keeps charts working with no network at
 * all. The cost is parsing ~750KB of JS per chart frame, which is a few tens
 * of milliseconds and only on cards that actually have a chart.
 *
 * chart-host.html stays the source of truth and still opens standalone. */
const HOST_ASSETS = [
  '/static/viz/chart-host.html',
  '/static/viz/vendor/vega.min.js',
  '/static/viz/vendor/vega-lite.min.js',
] as const;

let hostHtmlPromise: Promise<string> | null = null;

/** Inline a script body safely: a `</script>` inside a JS string literal would
 *  otherwise close the tag early and truncate the runtime. */
function inlineScript(source: string): string {
  return `<script>${source.replace(/<\/script/gi, '<\\/script')}<\/script>`;
}

function loadChartHost(): Promise<string> {
  hostHtmlPromise ??= Promise.all(
    HOST_ASSETS.map((url) =>
      fetch(url).then((r) => (r.ok ? r.text() : Promise.reject(new Error(url)))),
    ),
  )
    .then(([html, vega, vegaLite]) =>
      // Replacer FUNCTIONS, not strings: a replacement string would treat the
      // `$&` / `$'` sequences that occur in minified code as substitution
      // patterns and splice the surrounding HTML into the runtime.
      html
        .replace('<script src="/static/viz/vendor/vega.min.js"></script>', () =>
          inlineScript(vega),
        )
        .replace('<script src="/static/viz/vendor/vega-lite.min.js"></script>', () =>
          inlineScript(vegaLite),
        ),
    )
    .catch(() => {
      hostHtmlPromise = null;
      return '';
    });
  return hostHtmlPromise;
}

/**
 * One card for both inline visual kinds.
 *
 * `svg` diagrams render directly in the message: DOMPurify strips scripts and
 * event handlers, and what is left is styled by viz.css, so the diagram
 * inherits the active theme, keeps its text selectable, and costs no runtime.
 *
 * `chart` specs render in a `sandbox="allow-scripts"` iframe. A model-authored
 * spec is attacker-influenceable, so the Vega runtime that evaluates it stays
 * on an opaque origin with no reach into the app or the local backend.
 */
export const VizCard: React.FC<{ viz: VizArtifact }> = ({ viz }) => {
  const tokens = useThemeTokens();
  const svgHostRef = useRef<HTMLDivElement>(null);
  const frameRef = useRef<HTMLIFrameElement>(null);
  const [frameHeight, setFrameHeight] = useState(320);
  const [chartError, setChartError] = useState<string | null>(null);
  const [hostHtml, setHostHtml] = useState<string | null>(null);
  const [flash, setFlash] = useState<string | null>(null);

  // Resolvers for in-flight postMessage export round trips, keyed by request id.
  const pending = useRef(new Map<number, (data: string | null) => void>());
  const nextId = useRef(1);

  const clean = useMemo(
    () =>
      viz.kind === 'svg'
        ? DOMPurify.sanitize(viz.source, { USE_PROFILES: { svg: true, svgFilters: true } })
        : '',
    [viz.kind, viz.source],
  );

  const spec = useMemo(() => {
    if (viz.kind !== 'chart') return null;
    try {
      return JSON.parse(viz.source) as Record<string, unknown>;
    } catch {
      return null;
    }
  }, [viz.kind, viz.source]);

  useEffect(() => {
    if (viz.kind !== 'chart') return;
    let live = true;
    void loadChartHost().then((html) => {
      if (live) setHostHtml(html || null);
    });
    return () => {
      live = false;
    };
  }, [viz.kind]);

  // ---- chart host bridge -------------------------------------------------
  useEffect(() => {
    if (viz.kind !== 'chart') return;
    const onMessage = (e: MessageEvent) => {
      if (!frameRef.current || e.source !== frameRef.current.contentWindow) return;
      const m = e.data as { type?: string; height?: number; message?: string; id?: number; data?: string | null };
      if (!m || typeof m !== 'object') return;
      if (m.type === 'ready') {
        setChartError(null);
        frameRef.current.contentWindow?.postMessage({ type: 'render', spec, tokens }, '*');
      } else if (m.type === 'height' && typeof m.height === 'number') {
        setFrameHeight(Math.max(80, m.height));
      } else if (m.type === 'error') {
        setChartError(m.message ?? 'unknown error');
      } else if (m.type === 'exported' && typeof m.id === 'number') {
        pending.current.get(m.id)?.(m.data ?? null);
        pending.current.delete(m.id);
      }
    };
    window.addEventListener('message', onMessage);
    return () => window.removeEventListener('message', onMessage);
  }, [viz.kind, spec, tokens]);

  // Re-theme (and re-render) an already-loaded host when the theme changes.
  useEffect(() => {
    if (viz.kind !== 'chart' || !spec) return;
    frameRef.current?.contentWindow?.postMessage({ type: 'render', spec, tokens }, '*');
  }, [viz.kind, spec, tokens]);

  const requestFromHost = useCallback((format: ExportFormat): Promise<string | null> => {
    const win = frameRef.current?.contentWindow;
    if (!win) return Promise.resolve(null);
    const id = nextId.current++;
    return new Promise((resolve) => {
      pending.current.set(id, resolve);
      win.postMessage({ type: 'export', format, id }, '*');
      // Never leave a button spinning if the host dies mid-request.
      window.setTimeout(() => {
        if (pending.current.delete(id)) resolve(null);
      }, 5000);
    });
  }, []);

  // ---- exports -----------------------------------------------------------
  const showFlash = useCallback((text: string) => {
    setFlash(text);
    window.setTimeout(() => setFlash(null), 1400);
  }, []);

  /** The one place both kinds converge: SVG markup plus its intrinsic size. */
  const getMarkup = useCallback(async () => {
    if (viz.kind === 'chart') {
      const markup = await requestFromHost('svg');
      if (!markup) return null;
      const probe = new DOMParser().parseFromString(markup, 'image/svg+xml')
        .documentElement as unknown as SVGSVGElement;
      return { markup, size: svgSize(probe) };
    }
    const live = svgHostRef.current?.querySelector('svg');
    if (!live) return null;
    const node = live as SVGSVGElement;
    return { markup: serializeSvg(standaloneSvg(node, tokens.bg)), size: svgSize(node) };
  }, [viz.kind, requestFromHost, tokens.bg]);

  const handleSvg = useCallback(async () => {
    const out = await getMarkup();
    if (!out) return showFlash('Failed');
    downloadFile(out.markup, `${slugify(viz.title, 'visual')}.svg`, 'image/svg+xml');
    showFlash('Saved');
  }, [getMarkup, showFlash, viz.title]);

  const getPng = useCallback(async (): Promise<Blob | null> => {
    // Vega rasterises through its own canvas renderer, which handles chart
    // fonts and clipping better than re-rasterising its SVG output would.
    if (viz.kind === 'chart') {
      const dataUrl = await requestFromHost('png');
      return dataUrl ? dataUrlToBlob(dataUrl) : null;
    }
    const out = await getMarkup();
    return out ? svgToPngBlob(out.markup, out.size, 2) : null;
  }, [viz.kind, requestFromHost, getMarkup]);

  const handlePng = useCallback(async () => {
    const blob = await getPng();
    if (!blob) return showFlash('Failed');
    downloadFile(blob, `${slugify(viz.title, 'visual')}.png`);
    showFlash('Saved');
  }, [getPng, showFlash, viz.title]);

  const handleCopy = useCallback(async () => {
    const blob = await getPng();
    if (!blob) return showFlash('Failed');
    showFlash((await copyPngToClipboard(blob)) ? 'Copied' : 'Failed');
  }, [getPng, showFlash]);

  return (
    <div className="viz-card">
      <div className="viz-card-head">
        <div className="viz-card-titles">
          <div className="viz-card-title">{viz.title}</div>
          {viz.description && <div className="viz-card-desc">{viz.description}</div>}
        </div>
        <div className="viz-card-actions">
          {flash && <span className="viz-card-desc">{flash}</span>}
          <button className="btn-sm" type="button" onClick={handleSvg}>SVG</button>
          <button className="btn-sm" type="button" onClick={handlePng}>PNG</button>
          <button className="btn-sm" type="button" onClick={handleCopy}>Copy</button>
        </div>
      </div>

      {viz.kind === 'svg' ? (
        <div className="viz-card-body">
          {/* Sanitised above with DOMPurify's SVG profile: no <script>, no
              on* handlers, no foreignObject escape hatch. */}
          <div ref={svgHostRef} className="viz-svg" dangerouslySetInnerHTML={{ __html: clean }} />
        </div>
      ) : spec === null ? (
        <div className="viz-error">Chart spec was not valid JSON.</div>
      ) : (
        <div className="viz-card-body is-chart">
          {chartError && <div className="viz-error">{chartError}</div>}
          {hostHtml && (
            <iframe
              ref={frameRef}
              className="viz-frame"
              srcDoc={hostHtml}
              sandbox="allow-scripts"
              style={{ height: frameHeight }}
              title={viz.title}
            />
          )}
        </div>
      )}
    </div>
  );
};
