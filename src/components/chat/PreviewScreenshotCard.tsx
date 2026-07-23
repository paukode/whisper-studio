import React from 'react';

interface PreviewScreenshotCardProps {
  mediaType: string;
  data: string;
  caption?: string;
}

/** Renders a preview_screenshot result as an actual image — the same
 *  screenshot the model received as an image content block, not a wall of
 *  base64 text in a <pre>. */
export const PreviewScreenshotCard: React.FC<PreviewScreenshotCardProps> = ({ mediaType, data, caption }) => {
  const src = `data:${mediaType};base64,${data}`;
  return (
    <div
      className="preview-screenshot-card"
      style={{ border: '1px solid var(--border)', borderRadius: '8px', overflow: 'hidden' }}
    >
      {caption && (
        <div
          style={{
            padding: '6px 10px',
            fontSize: '0.8em',
            color: 'var(--text-muted)',
            background: 'var(--surface-1, transparent)',
            borderBottom: '1px solid var(--border)',
          }}
        >
          {caption}
        </div>
      )}
      <img
        src={src}
        alt={caption || 'Preview screenshot'}
        style={{ display: 'block', width: '100%', maxHeight: '480px', objectFit: 'contain', background: '#111' }}
      />
    </div>
  );
};
