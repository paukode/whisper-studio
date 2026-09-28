import React from 'react';
import type { MediaSize } from '@/types/chat';
import { MediaResizeGrip, mediaWidthStyle, useMediaResize } from './MediaResize';

interface PreviewScreenshotCardProps {
  mediaType: string;
  data: string;
  caption?: string;
  /** The size the user dragged the screenshot to, if any. */
  size?: MediaSize;
  /** Save a dragged size, or null for the natural one. Without it the card
   *  has no resize grip. */
  onResize?: (size: MediaSize | null) => void;
}

const noResize = () => {};

/** Renders a preview_screenshot result as an actual image — the same
 *  screenshot the model received as an image content block, not a wall of
 *  base64 text in a <pre>. At its natural size a tall shot is capped at
 *  480px; once resized it shows whole at the width the user chose. */
export const PreviewScreenshotCard: React.FC<PreviewScreenshotCardProps> = ({
  mediaType,
  data,
  caption,
  size: savedSize,
  onResize,
}) => {
  const src = `data:${mediaType};base64,${data}`;
  const { frameRef, size, resizing, gripProps } = useMediaResize({
    saved: savedSize,
    keepRatio: true,
    onChange: onResize ?? noResize,
  });
  return (
    <div
      ref={frameRef}
      className={`preview-screenshot-card${size ? ' media-sized' : ''}${resizing ? ' is-resizing' : ''}`}
      style={{
        position: 'relative',
        border: '1px solid var(--border)',
        borderRadius: '8px',
        overflow: 'hidden',
        ...mediaWidthStyle(size),
      }}
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
        style={{
          display: 'block',
          width: '100%',
          maxHeight: size ? 'none' : '480px',
          objectFit: 'contain',
          background: '#111',
        }}
      />
      {onResize && <MediaResizeGrip {...gripProps} />}
    </div>
  );
};
