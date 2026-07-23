import React, { useMemo } from 'react';

export interface CSVViewerProps {
  filePath: string;
  content: string;
  /** Called when "Edit Raw" is clicked — parent switches to Monaco. */
  onEditRaw?: () => void;
}

/** Parse a single CSV line respecting quoted fields. */
function parseCSVLine(line: string, sep: string): string[] {
  const fields: string[] = [];
  let current = '';
  let inQuotes = false;
  for (let i = 0; i < line.length; i++) {
    const ch = line[i];
    if (inQuotes) {
      if (ch === '"' && line[i + 1] === '"') {
        current += '"';
        i++;
      } else if (ch === '"') {
        inQuotes = false;
      } else {
        current += ch;
      }
    } else {
      if (ch === '"') {
        inQuotes = true;
      } else if (ch === sep) {
        fields.push(current.trim());
        current = '';
      } else {
        current += ch;
      }
    }
  }
  fields.push(current.trim());
  return fields;
}

/**
 * CSV/TSV table viewer matching the original ws-spreadsheet-viewer.
 * Parses content into a table with row numbers. Has "Edit Raw" toggle.
 */
export const CSVViewer: React.FC<CSVViewerProps> = ({ filePath, content, onEditRaw }) => {
  const fileName = filePath.split('/').pop() ?? filePath;
  const isTSV = fileName.toLowerCase().endsWith('.tsv');
  const sep = isTSV ? '\t' : ',';

  const rows = useMemo(() => {
    const lines = content.split('\n').filter((l) => l.trim().length > 0);
    return lines.map((line) => parseCSVLine(line, sep));
  }, [content, sep]);

  const header = rows[0] ?? [];
  const body = rows.slice(1);

  return (
    <div className="ws-spreadsheet-viewer" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      <div className="ws-csv-info" style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 8px', borderBottom: '1px solid var(--border)', flexShrink: 0 }}>
        <span style={{ fontSize: '0.85em', color: 'var(--text-muted)' }}>{rows.length} rows</span>
        <button className="btn btn-sm" onClick={() => onEditRaw?.()} type="button" style={{ marginLeft: 'auto' }}>
          Edit Raw
        </button>
      </div>
      <div className="ws-spreadsheet-table-wrap" style={{ flex: 1, overflow: 'auto' }}>
        <table className="ws-spreadsheet-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85em' }}>
          <thead>
            <tr>
              <th style={{ padding: '4px 8px', borderBottom: '2px solid var(--border)', textAlign: 'left', position: 'sticky', top: 0, background: 'var(--bg-secondary)', color: 'var(--text-muted)', minWidth: 40 }}>#</th>
              {header.map((h, i) => (
                <th key={i} style={{ padding: '4px 8px', borderBottom: '2px solid var(--border)', textAlign: 'left', position: 'sticky', top: 0, background: 'var(--bg-secondary)', whiteSpace: 'nowrap' }}>{h}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, ri) => (
              <tr key={ri}>
                <td className="ws-spreadsheet-rownum" style={{ padding: '2px 8px', borderBottom: '1px solid var(--border)', color: 'var(--text-muted)', fontSize: '0.9em' }}>{ri + 2}</td>
                {row.map((cell, ci) => (
                  <td key={ci} style={{ padding: '2px 8px', borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap' }}>{cell}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};
