import React, { useState, useCallback, useRef } from 'react';
import { openHtmlSandboxed } from '@/utils/openHtmlSandboxed';

interface ProgramArtifactCardProps {
  title: string;
  html: string;
  description: string;
}

/**
 * Inline chat card for a self-contained HTML program artifact.
 * Supports preview (iframe), open in new tab, and download.
 */
export const ProgramArtifactCard: React.FC<ProgramArtifactCardProps> = ({
  title,
  html,
  description,
}) => {
  const [showPreview, setShowPreview] = useState(false);
  const iframeRef = useRef<HTMLIFrameElement>(null);

  const handlePreview = useCallback(() => {
    setShowPreview((prev) => !prev);
  }, []);

  const handleDownload = useCallback(() => {
    const blob = new Blob([html], { type: 'text/html' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    const filename =
      title
        .toLowerCase()
        .replace(/[^a-z0-9]+/g, '-')
        .replace(/(^-|-$)/g, '') || 'program';
    a.download = `${filename}.html`;
    a.click();
    URL.revokeObjectURL(url);
  }, [html, title]);

  const handleOpenNewTab = useCallback(() => {
    // Open in a sandboxed iframe, never as a same-origin blob: document —
    // otherwise model-authored HTML could call the local backend API.
    openHtmlSandboxed(html);
  }, [html]);

  return (
    <div className="program-artifact-card" style={{
      border: '1px solid var(--border)',
      borderRadius: 8,
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
        <span style={{ fontSize: '1.1em' }}>{'\uD83D\uDCBB'}</span>
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
      {showPreview && (
        <div style={{ borderTop: '1px solid var(--border)' }}>
          <iframe
            ref={iframeRef}
            srcDoc={html}
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
