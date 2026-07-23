import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get } from '@/api/client';

interface FileEntry {
  path: string;
  size: number;
}

function formatSize(n: number): string {
  return n < 1024 ? `${n} B` : `${(n / 1024).toFixed(1)} KB`;
}

/** Read-only view of a folder skill's bundled files (scripts, references,
 *  assets). Clicking a file fetches and inlines its contents. */
export const SkillFileTree: React.FC<{ skillName: string }> = ({ skillName }) => {
  const { data } = useQuery({
    queryKey: ['skillFiles', skillName],
    queryFn: () =>
      get<{ files: FileEntry[] }>(`/api/skills/${encodeURIComponent(skillName)}/files`),
  });
  const [openPath, setOpenPath] = useState<string | null>(null);
  const [content, setContent] = useState('');
  const [binary, setBinary] = useState(false);

  const files = data?.files ?? [];

  const openFile = async (path: string) => {
    if (openPath === path) {
      setOpenPath(null);
      return;
    }
    try {
      const r = await get<{ content: string; binary: boolean; truncated: boolean }>(
        `/api/skills/${encodeURIComponent(skillName)}/file?path=${encodeURIComponent(path)}`,
      );
      setContent(r.content ?? '');
      setBinary(!!r.binary);
      setOpenPath(path);
    } catch (err) {
      console.warn('Failed to read skill file:', err);
    }
  };

  if (files.length === 0) return null;

  return (
    <div style={{ marginTop: '8px', fontSize: '13px' }}>
      <div style={{ opacity: 0.7, marginBottom: '4px' }}>Bundled files</div>
      <ul style={{ listStyle: 'none', margin: 0, padding: 0 }}>
        {files.map((f) => (
          <li key={f.path}>
            <button
              type="button"
              onClick={() => void openFile(f.path)}
              style={{
                display: 'flex',
                justifyContent: 'space-between',
                width: '100%',
                background: 'none',
                border: 'none',
                padding: '3px 6px',
                cursor: 'pointer',
                textAlign: 'left',
                color: 'inherit',
                fontFamily: 'var(--font-mono, monospace)',
              }}
            >
              <span>{f.path}</span>
              <span style={{ opacity: 0.5 }}>{formatSize(f.size)}</span>
            </button>
            {openPath === f.path && (
              <pre
                style={{
                  margin: '2px 6px 8px',
                  padding: '8px',
                  maxHeight: '280px',
                  overflow: 'auto',
                  background: 'var(--surface-0, rgba(127,127,127,0.08))',
                  borderRadius: '6px',
                  fontSize: '12px',
                  whiteSpace: 'pre-wrap',
                  wordBreak: 'break-word',
                }}
              >
                {binary ? '(binary file — not shown)' : content}
              </pre>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
};
