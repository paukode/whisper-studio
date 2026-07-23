import React from 'react';
import { rawFileUrl } from '@/api/workspace';

export interface PDFViewerProps {
  filePath: string;
  /** Override the raw-bytes URL (e.g. sourceFileRawUrl for indexed-folder
   *  citations). Defaults to the workspace-relative rawFileUrl. */
  rawUrl?: string;
}

/**
 * PDF viewer using embed element with browser's built-in PDF renderer.
 */
export const PDFViewer: React.FC<PDFViewerProps> = ({ filePath, rawUrl }) => {
  const fileName = filePath.split('/').pop() ?? filePath;
  const pdfUrl = rawUrl ?? rawFileUrl(filePath);

  return (
    <div className="ws-pdf-viewer" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="ws-pdf-toolbar" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 8px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
        <span style={{ fontSize: '0.85em', color: 'var(--text-secondary)' }}>{fileName}</span>
        <a
          href={pdfUrl}
          target="_blank"
          rel="noopener noreferrer"
          className="btn btn-sm"
          style={{ marginLeft: 'auto', textDecoration: 'none' }}
        >
          Open in new tab
        </a>
      </div>
      <div style={{ flex: 1, minHeight: 0 }}>
        <embed
          src={pdfUrl}
          type="application/pdf"
          style={{ width: '100%', height: '100%', border: 'none' }}
          title={`PDF: ${fileName}`}
        />
      </div>
    </div>
  );
};
