import React, { useCallback, useEffect, useRef, useState } from 'react';
import { rawFileUrl } from '@/api/workspace';

export interface ImageViewerProps {
  filePath: string;
  alt?: string;
  /** Override the raw-bytes URL (e.g. sourceFileRawUrl for indexed-folder
   *  citations). Defaults to the workspace-relative rawFileUrl. */
  rawUrl?: string;
}

/**
 * Image viewer matching the original ws-image-viewer:
 *   + / - / 1:1 buttons and wheel-scroll zoom.
 *   Uses width percentage sizing (not CSS transform) to avoid scroll area issues.
 */
export const ImageViewer: React.FC<ImageViewerProps> = ({ filePath, alt, rawUrl }) => {
  const [hasError, setHasError] = useState(false);
  const [zoom, setZoom] = useState(1);
  const fileName = filePath.split('/').pop() ?? filePath;
  const imageUrl = rawUrl ?? rawFileUrl(filePath);

  const handleZoomIn = useCallback(() => setZoom((z) => Math.min(z + 0.25, 10)), []);
  const handleZoomOut = useCallback(() => setZoom((z) => Math.max(z - 0.25, 0.1)), []);
  const handleZoomReset = useCallback(() => setZoom(1), []);

  // Wheel-zoom must call preventDefault() to stop the page from scrolling
  // underneath the zoom. React attaches `onWheel` as a PASSIVE listener, where
  // preventDefault() is ignored — so bind it manually with { passive: false }
  // on the scroll container instead. Functional setZoom keeps the delta logic
  // closure-free, so the effect only needs to run once.
  const scrollRef = useRef<HTMLDivElement | null>(null);
  useEffect(() => {
    const el = scrollRef.current;
    if (!el) return;
    const handleWheel = (e: WheelEvent) => {
      e.preventDefault();
      setZoom((z) => {
        const delta = e.deltaY > 0 ? -0.15 : 0.15;
        return Math.max(0.1, Math.min(10, z + delta));
      });
    };
    el.addEventListener('wheel', handleWheel, { passive: false });
    return () => el.removeEventListener('wheel', handleWheel);
  }, []);

  if (hasError) {
    return (
      <div className="ws-image-viewer" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%' }}>
        <p style={{ color: 'var(--text-muted)' }}>Failed to load image: {fileName}</p>
      </div>
    );
  }

  return (
    <div className="ws-image-viewer" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="ws-image-controls" style={{ display: 'flex', alignItems: 'center', gap: 4, padding: '4px 8px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
        <button className="btn btn-sm" onClick={handleZoomOut} title="Zoom out" type="button">&minus;</button>
        <button className="btn btn-sm" onClick={handleZoomReset} title="Reset zoom (1:1)" type="button">1:1</button>
        <button className="btn btn-sm" onClick={handleZoomIn} title="Zoom in" type="button">+</button>
        <span style={{ fontSize: '0.8em', color: 'var(--text-muted)', minWidth: 40, textAlign: 'center' }}>
          {Math.round(zoom * 100)}%
        </span>
      </div>
      {/* `margin: auto` centers the img when it fits but, unlike flex centering,
          keeps zoomed overflow scrollable — centered flex content that overflows
          past the start edge is unreachable (can't scroll to it). */}
      <div ref={scrollRef} style={{ flex: 1, overflow: 'auto', display: 'flex', padding: 16 }}>
        <img
          src={imageUrl}
          alt={alt ?? fileName}
          className="ws-image-preview"
          style={{
            margin: 'auto',
            maxWidth: zoom === 1 ? '100%' : 'none',
            maxHeight: zoom === 1 ? '100%' : 'none',
            width: zoom !== 1 ? `${zoom * 100}%` : undefined,
            objectFit: 'contain',
            imageRendering: zoom > 2 ? 'pixelated' : 'auto',
          }}
          onError={() => setHasError(true)}
          draggable={false}
        />
      </div>
    </div>
  );
};
