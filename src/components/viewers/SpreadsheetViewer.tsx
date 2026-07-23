import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import type { Workbook, Worksheet, Row, Cell, CellValue } from 'exceljs';
import { rawFileUrl } from '@/api/workspace';
import { toError } from '@/utils/toError';

export interface SpreadsheetViewerProps {
  filePath: string;
  /** Override the raw-bytes URL (e.g. sourceFileRawUrl for indexed-folder
   *  citations). Defaults to the workspace-relative rawFileUrl. */
  rawUrl?: string;
}

interface SheetData {
  sheetNames: string[];
  sheets: Record<string, string[][]>;
}

/**
 * Spreadsheet viewer using exceljs.
 *
 * Migrated from xlsx@0.18.5 which carried CVE-2023-30533 (proto
 * pollution) and CVE-2024-22363 (ReDoS) — both trigger on parsing a
 * malicious .xlsx upload. exceljs has neither, is maintained, and the
 * resulting shape (`{sheetNames, sheets: name -> rows of strings}`)
 * is identical to what the previous code produced so the table render
 * below is unchanged.
 */
export const SpreadsheetViewer: React.FC<SpreadsheetViewerProps> = ({ filePath, rawUrl }) => {
  const fileName = filePath.split('/').pop() ?? filePath;
  // User's tab selection. The effective active sheet is derived below so it
  // gracefully falls back to the first sheet (incl. when switching files).
  const [selectedSheet, setSelectedSheet] = useState<string>('');

  // Keyed on the effective URL, not just filePath: the same path can be fetched
  // through /file (workspace tab) or /source-file (dock citation), and the two
  // endpoints must not share a cache entry.
  const effectiveUrl = rawUrl ?? rawFileUrl(filePath);
  const { data, error, isLoading } = useQuery({
    queryKey: ['ws-spreadsheet', effectiveUrl],
    // Lazy import keeps exceljs out of the main bundle — only loaded when a
    // .xlsx is actually opened.
    queryFn: async (): Promise<SheetData> => {
      const res = await fetch(effectiveUrl);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      const buf = await res.arrayBuffer();
      const ExcelJS = await import('exceljs');
      const wb: Workbook = new ExcelJS.Workbook();
      await wb.xlsx.load(buf);
      const sheetNames: string[] = [];
      const sheets: Record<string, string[][]> = {};
      wb.worksheets.forEach((ws: Worksheet) => {
        sheetNames.push(ws.name);
        sheets[ws.name] = worksheetToRows(ws);
      });
      return { sheetNames, sheets };
    },
    staleTime: 30_000,
  });

  const handleSheetClick = useCallback((name: string) => {
    setSelectedSheet(name);
  }, []);

  if (isLoading) {
    return (
      <div className="ws-spreadsheet-viewer" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
        <span>Loading spreadsheet…</span>
      </div>
    );
  }

  if (error) {
    return (
      <div className="ws-spreadsheet-viewer" style={{ display: 'flex', alignItems: 'center', justifyContent: 'center', height: '100%', color: 'var(--text-muted)' }}>
        <p>Failed to load spreadsheet: {toError(error).message || 'Failed to load spreadsheet'}</p>
      </div>
    );
  }

  if (!data) return null;

  // Effective active sheet: the user's pick if it still exists in this file,
  // else the first sheet (handles initial render + switching files).
  const activeSheet = (selectedSheet && data.sheets[selectedSheet])
    ? selectedSheet
    : (data.sheetNames[0] ?? '');
  const rows = data.sheets[activeSheet] ?? [];
  const header = rows[0] ?? [];
  const body = rows.slice(1);

  return (
    <div className="ws-spreadsheet-viewer" style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
      {/* Sheet tabs — only if multiple sheets */}
      {data.sheetNames.length > 1 && (
        <div className="ws-sheet-tabs" style={{ display: 'flex', gap: 0, borderBottom: '1px solid var(--border)', flexShrink: 0, overflow: 'auto' }}>
          {data.sheetNames.map((name) => (
            <button
              key={name}
              className={`btn btn-sm ws-sheet-tab${name === activeSheet ? ' active' : ''}`}
              onClick={() => handleSheetClick(name)}
              type="button"
              style={{
                padding: '4px 12px',
                borderRadius: 0,
                borderBottom: name === activeSheet ? '2px solid var(--accent)' : '2px solid transparent',
                background: name === activeSheet ? 'var(--bg-primary)' : 'transparent',
                fontWeight: name === activeSheet ? 600 : 400,
              }}
            >
              {name}
            </button>
          ))}
        </div>
      )}

      {/* Info bar */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '4px 8px', borderBottom: '1px solid var(--border)', flexShrink: 0, fontSize: '0.85em', color: 'var(--text-muted)' }}>
        <span>{fileName}</span>
        <span>{rows.length} rows</span>
      </div>

      {/* Table */}
      <div style={{ flex: 1, overflow: 'auto' }}>
        <table className="ws-spreadsheet-table" style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85em' }}>
          <thead>
            <tr>
              <th style={{ padding: '4px 8px', borderBottom: '2px solid var(--border)', textAlign: 'left', position: 'sticky', top: 0, background: 'var(--bg-secondary)', color: 'var(--text-muted)', minWidth: 40 }}>#</th>
              {header.map((h, i) => (
                <th key={i} style={{ padding: '4px 8px', borderBottom: '2px solid var(--border)', textAlign: 'left', position: 'sticky', top: 0, background: 'var(--bg-secondary)', whiteSpace: 'nowrap' }}>{String(h)}</th>
              ))}
            </tr>
          </thead>
          <tbody>
            {body.map((row, ri) => (
              <tr key={ri}>
                <td className="ws-spreadsheet-rownum" style={{ padding: '2px 8px', borderBottom: '1px solid var(--border)', color: 'var(--text-muted)', fontSize: '0.9em' }}>{ri + 2}</td>
                {row.map((cell, ci) => (
                  <td key={ci} style={{ padding: '2px 8px', borderBottom: '1px solid var(--border)', whiteSpace: 'nowrap' }}>{String(cell)}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
};

/**
 * Convert an exceljs worksheet to the legacy `string[][]` shape this
 * component already knows how to render. Walks every cell across the
 * worksheet's used range so empty cells render as '' rather than
 * collapsing the row.
 *
 * Cell value coercion mirrors what `xlsx.utils.sheet_to_json` produced
 * for the same inputs:
 *   - null/undefined → ''
 *   - Date → ISO string
 *   - { richText: [...] } → joined .text
 *   - { formula, result } → result (or formula if no result yet)
 *   - { hyperlink, text } → text
 *   - { error } → error code string
 *   - everything else (string, number, bool) → String(value)
 */
function worksheetToRows(ws: Worksheet): string[][] {
  const out: string[][] = [];
  const colCount = ws.columnCount;
  if (ws.rowCount === 0 || colCount === 0) return out;

  ws.eachRow({ includeEmpty: true }, (row: Row) => {
    const cells: string[] = [];
    for (let c = 1; c <= colCount; c++) {
      const cell: Cell = row.getCell(c);
      cells.push(cellToString(cell.value));
    }
    out.push(cells);
  });
  return out;
}

function cellToString(value: CellValue): string {
  if (value === null || value === undefined) return '';
  if (value instanceof Date) return value.toISOString();
  if (typeof value === 'string' || typeof value === 'number' || typeof value === 'boolean') {
    return String(value);
  }
  // Object-valued cells (rich text, formulas, hyperlinks, errors).
  // exceljs's union of object cell types doesn't share an index
  // signature, so cast through unknown to access fields by name.
  if (typeof value === 'object') {
    const v = value as unknown as Record<string, unknown>;
    if (Array.isArray(v.richText)) {
      return (v.richText as Array<{ text?: string }>).map((r) => r.text ?? '').join('');
    }
    if ('result' in v && v.result !== undefined && v.result !== null) {
      // Formula cells expose the cached calculated result.
      return cellToString(v.result as CellValue);
    }
    if ('formula' in v && typeof v.formula === 'string') {
      return v.formula;
    }
    if ('hyperlink' in v && typeof v.text === 'string') {
      return v.text;
    }
    if ('error' in v && typeof v.error === 'string') {
      return v.error;
    }
    if ('text' in v && typeof v.text === 'string') {
      return v.text;
    }
  }
  return String(value);
}
