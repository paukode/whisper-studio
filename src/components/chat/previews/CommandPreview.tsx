export interface CommandPreviewProps {
  command?: string;
  cwd?: string;
  /** The server could not render the command preview; shown on the card
   *  so a failed preview is never a blank body. Display only. */
  previewError?: string;
}

export function CommandPreview({ command, cwd, previewError }: CommandPreviewProps) {
  if (previewError) {
    return (
      <div role="alert" style={{ fontSize: 12, color: 'var(--danger)', whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
        {previewError}
      </div>
    );
  }
  // No command means the summary at the top of the banner is the
  // only meaningful preview: render nothing here to avoid a hollow
  // "$ (no command)" box.
  if (!command || !command.trim()) return null;
  return (
    <div>
      <pre style={{
        fontSize: 13, fontFamily: 'Menlo, monospace',
        background: 'var(--bg-inset)', padding: 10, borderRadius: 4,
        whiteSpace: 'pre-wrap', wordBreak: 'break-all', margin: 0,
      }}>$ {command}</pre>
      {cwd && (
        <div style={{ fontSize: 11, color: 'var(--text-muted)', marginTop: 4 }}>
          in {cwd}
        </div>
      )}
    </div>
  );
}
