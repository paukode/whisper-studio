import React, { useMemo } from 'react';

/**
 * The bundled documentation site in the dock, served by the backend at
 * /docs-site (see server/main.py). Opened by the header's ? button (docs
 * home) and by #docspage= reference links in @docs answers (a specific page
 * and heading). Re-keyed by navRev so a repeat click on the same reference
 * re-navigates the frame even when page and anchor are unchanged.
 */
export const DocsPanel: React.FC<{ page: string; anchor?: string; navRev?: number }> = ({
  page,
  anchor,
  navRev,
}) => {
  const src = useMemo(() => {
    const base = `/docs-site/${page}`;
    return anchor ? `${base}#${encodeURIComponent(anchor)}` : base;
  }, [page, anchor]);
  return (
    <iframe
      key={navRev ?? 0}
      src={src}
      title="Documentation"
      style={{
        border: 'none',
        width: '100%',
        flex: 1,
        minHeight: 0,
        // The docs site paints its own themed background; this only covers the
        // instant before it loads.
        background: 'var(--bg-primary, #fff)',
      }}
    />
  );
};
