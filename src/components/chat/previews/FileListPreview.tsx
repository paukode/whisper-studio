export interface FileListPreviewProps {
  paths?: string[];
}

export function FileListPreview({ paths }: FileListPreviewProps) {
  if (!paths || paths.length === 0) {
    return <div style={{ fontSize: 12, color: 'var(--text-muted)' }}>(no paths)</div>;
  }
  return (
    <ul style={{
      fontSize: 12, fontFamily: 'Menlo, monospace',
      background: 'var(--bg-inset)', padding: '8px 12px 8px 28px',
      borderRadius: 4, maxHeight: 240, overflow: 'auto', margin: 0,
    }}>
      {paths.map((p) => (
        <li key={p}>{p}</li>
      ))}
    </ul>
  );
}
