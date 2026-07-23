import React from 'react';
import { getLangForPath } from '@/utils/languageDetection';

export interface StatusBarProps {
  filePath: string | null;
  line: number;
  col: number;
  isBinary?: boolean;
}

/**
 * Status bar matching the original #wsStatusBar:
 *   Ln/Col | UTF-8 | Language
 */
export const StatusBar: React.FC<StatusBarProps> = ({ filePath, line, col, isBinary }) => {
  if (!filePath || isBinary) return null;

  const lang = getLangForPath(filePath);
  const langLabel = lang === 'plaintext'
    ? 'Plain Text'
    : lang.charAt(0).toUpperCase() + lang.slice(1);

  return (
    <div className="ws-status-bar" id="wsStatusBar">
      <span id="wsStatusCursor">Ln {line}, Col {col}</span>
      <span id="wsStatusEncoding">UTF-8</span>
      <span id="wsStatusLang">{langLabel}</span>
    </div>
  );
};
