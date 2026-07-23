export interface CommandPreviewProps {
  command?: string;
  cwd?: string;
}

export function CommandPreview({ command, cwd }: CommandPreviewProps) {
  // No command means the summary at the top of the banner is the
  // only meaningful preview — render nothing here to avoid a hollow
  // "$ (no command)" box.
  if (!command || !command.trim()) return null;
  return (
    <div>
      <pre style={{
        fontSize: 12, fontFamily: 'Menlo, monospace',
        background: 'var(--bg-inset)', padding: 10, borderRadius: 4,
        whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0,
      }}>$ {command}</pre>
      {cwd && (
        <div style={{ fontSize: 10, color: 'var(--text-muted)', marginTop: 4 }}>
          in {cwd}
        </div>
      )}
    </div>
  );
}
