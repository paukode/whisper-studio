import React, { useCallback, useMemo } from 'react';
import { renderMarkdownSafe } from '@/utils/sanitizeHtml';
import { useHtmlProp } from '@/hooks/useHtmlProp';
import { downloadFile } from '@/utils/downloadFile';

export interface MarkdownPreviewProps {
  filePath: string;
  content: string;
  onEdit?: () => void;
}

/**
 * Escape the characters that would break out of an HTML text/title context.
 * The filename is interpolated into a `<title>…</title>` in the popup/download
 * documents below; a name containing `<`, `>` or `&` (workspace files can be
 * named anything) would otherwise corrupt the markup or inject nodes.
 */
export function escapeHtml(s: string): string {
  return s.replace(/[<>&]/g, (c) => ({ '<': '&lt;', '>': '&gt;', '&': '&amp;' }[c]!));
}

/**
 * Markdown preview (ws-markdown-viewer): a toolbar with Preview (active) +
 * Edit, plus open-in-tab and download actions. The caller passes the content.
 *
 * Safety: the HTML is sanitized by DOMPurify (renderMarkdownSafe), because a
 * workspace file can come from anywhere.
 */
export const MarkdownPreview: React.FC<MarkdownPreviewProps> = ({ filePath, content, onEdit }) => {
  const fileName = filePath.split('/').pop() ?? filePath;

  // Workspace .md files can be authored externally (cloned repos, file shares,
  // AI-generated content the user accepted). Sanitize via DOMPurify so an
  // embedded <img onerror> can't fire on open. Derived value, not state.
  const html = useMemo(() => renderMarkdownSafe(content), [content]);

  const handleOpenInNewTab = useCallback(() => {
    const win = window.open('', '_blank');
    if (win) {
      const title = escapeHtml(fileName);
      win.document.write(`<!DOCTYPE html><html><head><title>${title}</title></head><body>${html}</body></html>`);
      win.document.close();
    }
  }, [fileName, html]);

  const handleDownloadHtml = useCallback(() => {
    const title = escapeHtml(fileName);
    downloadFile(
      `<!DOCTYPE html><html><head><title>${title}</title></head><body>${html}</body></html>`,
      fileName.replace(/\.(md|mdx)$/i, '.html'),
      'text/html',
    );
  }, [fileName, html]);

  return (
    <div className="ws-markdown-viewer">
      <div className="ws-markdown-toolbar">
        <button className="btn btn-sm active" type="button" disabled>Preview</button>
        {onEdit && (
          <button className="btn btn-sm" onClick={onEdit} type="button">Edit</button>
        )}
        <button
          className="btn btn-sm"
          onClick={handleOpenInNewTab}
          type="button"
          title="Open in new tab"
          aria-label="Open in new tab"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/>
            <polyline points="15 3 21 3 21 9"/>
            <line x1="10" y1="14" x2="21" y2="3"/>
          </svg>
        </button>
        <button
          className="btn btn-sm"
          onClick={handleDownloadHtml}
          type="button"
          title="Download as HTML"
          aria-label="Download as HTML"
        >
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/>
            <polyline points="7 10 12 15 17 10"/>
            <line x1="12" y1="15" x2="12" y2="3"/>
          </svg>
        </button>
      </div>
      <MarkdownContent html={html} />
    </div>
  );
};

/** Renders markdown HTML already sanitized by renderMarkdownSafe (DOMPurify). */
const MarkdownContent: React.FC<{ html: string }> = ({ html }) => {
  const innerHtml = useHtmlProp(html);
  return <div className="ws-markdown-preview markdown-body" dangerouslySetInnerHTML={innerHtml} />;
};
