import React from 'react';

export interface BreadcrumbProps {
  path: string | null;
}

/**
 * Breadcrumb path display matching the original #wsEditorBreadcrumb.
 * Splits path by "/" and highlights the last segment.
 */
export const Breadcrumb: React.FC<BreadcrumbProps> = ({ path }) => {
  if (!path) return null;

  const parts = path.split('/');

  return (
    <div className="ws-editor-breadcrumb" id="wsEditorBreadcrumb">
      {parts.map((part, i) => (
        <React.Fragment key={i}>
          {i > 0 && <span className="ws-breadcrumb-sep"> / </span>}
          <span className={i === parts.length - 1 ? 'ws-breadcrumb-current' : undefined}>
            {part}
          </span>
        </React.Fragment>
      ))}
    </div>
  );
};
