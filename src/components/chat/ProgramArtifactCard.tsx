import React, { useState, useCallback, useEffect, useMemo, useReducer, useRef } from 'react';
import { openHtmlSandboxed } from '@/utils/openHtmlSandboxed';
import { downloadFile } from '@/utils/downloadFile';
import {
  artifactThemeCss,
  loadArtifactFonts,
  readArtifactVars,
  withArtifactTheme,
} from '@/utils/artifactTheme';
import { ICON } from '@/components/chat/activityIcons';

interface ProgramArtifactCardProps {
  title: string;
  html: string;
  description: string;
}

/** The theme block for an open preview, or null while closed or before the
 *  bundled fonts are read. A mode or palette switch re-renders the card, so
 *  the preview reloads in the new colors. */
function useArtifactThemeCss(active: boolean): string | null {
  const [fonts, setFonts] = useState<string | null>(null);
  const [, rerender] = useReducer((n: number) => n + 1, 0);
  // Read once per app session and shared by every card. Started on mount,
  // so Open and Download find it ready and open the tab inside the click.
  useEffect(() => {
    let live = true;
    loadArtifactFonts().then((css) => {
      if (live) setFonts(css);
    });
    return () => {
      live = false;
    };
  }, []);
  useEffect(() => {
    if (!active) return;
    const observer = new MutationObserver(rerender);
    observer.observe(document.documentElement, {
      attributes: true,
      attributeFilter: ['data-theme', 'data-palette'],
    });
    return () => observer.disconnect();
  }, [active]);
  if (!active || fonts === null) return null;
  return fonts + readArtifactVars();
}

/**
 * Inline chat card for a self-contained HTML program artifact.
 * Supports preview (iframe), open in new tab, and download, each with the
 * app theme added (src/utils/artifactTheme.ts).
 */
export const ProgramArtifactCard: React.FC<ProgramArtifactCardProps> = ({
  title,
  html,
  description,
}) => {
  const [showPreview, setShowPreview] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);
  const themeCss = useArtifactThemeCss(showPreview);
  const previewHtml = useMemo(
    () => (themeCss === null ? null : withArtifactTheme(html, themeCss)),
    [html, themeCss],
  );

  const handlePreview = useCallback(() => {
    setShowPreview((prev) => !prev);
  }, []);

  const handleDownload = useCallback(async () => {
    const filename =
      title
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/(^-|-$)/g, '') || 'program';
    downloadFile(withArtifactTheme(html, await artifactThemeCss()), `${filename}.html`, 'text/html');
  }, [html, title]);

  const handleOpenNewTab = useCallback(async () => {
    // Open in a sandboxed iframe, never as a same-origin blob: document;
    // otherwise model-authored HTML could call the local backend API.
    openHtmlSandboxed(withArtifactTheme(html, await artifactThemeCss()));
  }, [html]);

  return (
    <div className="program-artifact-card" style={{
      border: '1px solid var(--border)',
      borderRadius: 'var(--radius-sm)',
      overflow: 'hidden',
      margin: '8px 0',
    }}>
      {/* Header */}
      <div style={{
        padding: '10px 14px',
        background: 'var(--bg-secondary)',
        display: 'flex',
        alignItems: 'center',
        gap: 10,
      }}>
        <span style={{ display: 'inline-flex', color: 'var(--text-muted)' }}>{ICON.monitor}</span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ fontWeight: 600, fontSize: '0.9em' }}>{title}</div>
          {description && (
            <div style={{
              fontSize: '0.78em',
              color: 'var(--text-muted)',
              marginTop: 2,
              overflow: 'hidden',
              textOverflow: 'ellipsis',
              whiteSpace: 'nowrap',
            }}>
              {description}
            </div>
          )}
        </div>
        <button onClick={handlePreview} className="btn-sm" type="button">
          {showPreview ? 'Hide' : 'Preview'}
        </button>
        <button onClick={handleOpenNewTab} className="btn-sm" type="button">
          Open
        </button>
        <button onClick={handleDownload} className="btn-sm" type="button">
          Download
        </button>
      </div>

      {/* Preview iframe */}
      {showPreview && previewHtml !== null && (
        <div style={{ borderTop: '1px solid var(--border)' }}>
          <iframe
            ref={iframeRef}
            srcDoc={previewHtml}
            sandbox="allow-scripts"
            style={{
              width: '100%',
              height: 480,
              border: 'none',
              background: '#fff',
            }}
            title={title}
          />
        </div>
      )}
    </div>
  );
};
