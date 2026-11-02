import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { rawFileUrl } from '@/api/workspace';
import { toError } from '@/utils/toError';
import { sanitizeHtml } from '@/utils/sanitizeHtml';
import { useHtmlProp } from '@/hooks/useHtmlProp';

export interface WordViewerProps {
  filePath: string;
  /** Opens the document in its default app (Word or Pages). The app has no
   *  editor for a .docx: its text editor would save plain text over it. */
  onOpenInDefaultApp?: () => void;
  /** Override the raw-bytes URL (e.g. sourceFileRawUrl for indexed-folder
   *  citations). Defaults to the workspace-relative rawFileUrl. */
  rawUrl?: string;
}

/**
 * Word document viewer using mammoth.js.
 * Fetches the raw binary, converts to HTML, renders in a scrollable div.
 *
 * Safety: a .docx can come from anywhere, so mammoth's HTML is sanitized
 * (sanitizeHtml, DOMPurify) before it is rendered.
 */
export const WordViewer: React.FC<WordViewerProps> = ({ filePath, onOpenInDefaultApp, rawUrl }) => {
  const fileName = filePath.split('/').pop() ?? filePath;

  // react-query owns the fetch + convert lifecycle (loading/error/data) so
  // there's no setState-in-effect. The DOMPurify sanitize runs inside the
  // queryFn — defense in depth on top of mammoth's own controlled-subset output.
  // Keyed on the effective URL, not just filePath: the same path can be fetched
  // through /file (workspace tab) or /source-file (dock citation), and the two
  // endpoints must not share a cache entry.
  const effectiveUrl = rawUrl ?? rawFileUrl(filePath);
  const { data: html = '', error, isLoading } = useQuery({
    queryKey: ['ws-word-doc', effectiveUrl],
    queryFn: async () => {
      const res = await fetch(effectiveUrl);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = await res.arrayBuffer();
      const mammoth = await import('mammoth');
      const result = await mammoth.convertToHtml({ arrayBuffer: buf });
      return sanitizeHtml(result.value);
    },
    staleTime: 30_000,
  });

  if (isLoading) {
    return (
      <div className="ws-word-viewer" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
        <span>Loading document...</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="ws-word-viewer" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
        <p>Failed to load document: {toError(error).message || 'Failed to load document'}</p>
      </div>
    );
  }

  return (
    <div className="ws-word-viewer" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="ws-markdown-toolbar">
        <button className="btn btn-sm active" type="button" disabled>Preview</button>
        {onOpenInDefaultApp && (
          <button className="btn btn-sm" onClick={onOpenInDefaultApp} type="button">Open in default app</button>
        )}
        <span style={{ flex: 1 }} />
        <span style={{ fontSize: '12px', color: 'var(--text-muted)' }}>{fileName}</span>
      </div>
      <WordHtmlContent html={html} />
    </div>
  );
};

/** Renders mammoth's HTML, already sanitized by sanitizeHtml (DOMPurify). */
const WordHtmlContent: React.FC<{ html: string }> = ({ html }) => {
  const innerHtml = useHtmlProp(html);
  return <div className="ws-word-content markdown-body" style={{ flex: 1, overflow: 'auto', padding: 16 }} dangerouslySetInnerHTML={innerHtml} />;
};
