export interface TextPreviewProps {
  text?: string;
}

export function TextPreview({ text }: TextPreviewProps) {
  if (!text) return null;
  return (
    <pre style={{
      fontSize: 13, fontFamily: 'Menlo, monospace',
      background: 'var(--bg-inset)', padding: 8, borderRadius: 4,
      whiteSpace: 'pre-wrap', wordBreak: 'break-word', margin: 0,
    }}>{text}</pre>
  );
}
