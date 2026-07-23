import { useMemo } from 'react';
import { computeLineDiff } from '@/utils/computeLineDiff';

export interface DiffPreviewProps {
  /** May be undefined for create (no original) */
  original?: string;
  content?: string;
  path?: string;
}

export function DiffPreview({ original, content, path }: DiffPreviewProps) {
  const diffLines = useMemo(() => {
    if (original == null && content == null) return [];
    try {
      return computeLineDiff(original ?? '', content ?? '');
    } catch {
      return [];
    }
  }, [original, content]);

  const added = diffLines.filter((l) => l.type === 'added').length;
  const removed = diffLines.filter((l) => l.type === 'removed').length;

  return (
    <div>
      {path && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginBottom: 6, fontFamily: 'Menlo, monospace' }}>
          {path}
          {(added > 0 || removed > 0) && (
            <span style={{ marginLeft: 8 }}>
              <span style={{ color: 'var(--accent-live, #34c77b)' }}>+{added}</span>
              {' '}
              <span style={{ color: 'var(--accent-record, #e5453a)' }}>-{removed}</span>
            </span>
          )}
        </div>
      )}
      {diffLines.length === 0 ? (
        <pre style={{
          fontSize: 12, fontFamily: 'Menlo, monospace',
          background: 'var(--bg-inset)', padding: 8, borderRadius: 4,
          maxHeight: 240, overflow: 'auto', whiteSpace: 'pre-wrap',
        }}>{content ?? ''}</pre>
      ) : (
        <div style={{
          fontSize: 12, fontFamily: 'Menlo, monospace',
          background: 'var(--bg-inset)', padding: 8, borderRadius: 4,
          maxHeight: 240, overflow: 'auto',
        }}>
          {diffLines.map((line, i) => {
            const bg = line.type === 'added'
              ? 'color-mix(in srgb, var(--accent-live, #34c77b) 18%, transparent)'
              : line.type === 'removed'
                ? 'color-mix(in srgb, var(--accent-record, #e5453a) 18%, transparent)'
                : 'transparent';
            const prefix = line.type === 'added' ? '+ ' : line.type === 'removed' ? '- ' : '  ';
            return (
              <div key={i} style={{ background: bg, padding: '0 4px', whiteSpace: 'pre' }}>
                {prefix}{line.text}
              </div>
            );
          })}
        </div>
      )}
    </div>
  );
}
