import React, { useMemo, useState } from 'react';
import type { UsageRow, UsageSplit } from '@/types/costs';
import { formatTokenCount } from '@/components/chat/TokenCounter';
import { noteId, seriesClass, slotOf, type ChartSeries } from './usageSeries';
import { fmtCount, fmtFraction, fmtMoney } from './usageView';

type Column =
  | 'name'
  | 'cost_usd'
  | 'share'
  | 'prompt_tokens'
  | 'cached_pct'
  | 'output_tokens'
  | 'calls';

const COLUMNS: ReadonlyArray<{ id: Column; label: string; numeric: boolean }> = [
  { id: 'name', label: 'Name', numeric: false },
  { id: 'cost_usd', label: 'Cost', numeric: true },
  { id: 'share', label: 'Share', numeric: true },
  { id: 'prompt_tokens', label: 'Prompt', numeric: true },
  { id: 'cached_pct', label: 'Cached', numeric: true },
  { id: 'output_tokens', label: 'Output', numeric: true },
  { id: 'calls', label: 'Calls', numeric: true },
];

const SPLIT_NOUN: Record<UsageSplit, string> = { model: 'Model', session: 'Session', source: 'Source' };

interface Sort {
  column: Column;
  dir: 'asc' | 'desc';
}

const DEFAULT_SORT: Sort = { column: 'cost_usd', dir: 'desc' };

interface UsageTableProps {
  rows: UsageRow[];
  split: UsageSplit;
  nameOf: (row: UsageRow) => string;
  /** The chart's series, so each row wears its bar colour. */
  series: ChartSeries[];
  /** The report's pricing notes in their numbered order. */
  notes: string[];
}

/**
 * One row per split key, sorted on the client by any column header (Cost,
 * descending, to start). Token counts read compact, as the tiles do, with the
 * exact count on hover. A row whose token counts include estimates shows
 * every figure derived from them as estimated (cost, share, prompt tokens,
 * cached share and output; only the call count is exact), the same way the
 * summary tiles do. A row with a pricing note (the GPT list-rate note) points
 * at it by number instead of repeating it: the notes above list each once.
 */
export const UsageTable: React.FC<UsageTableProps> = ({ rows, split, nameOf, series, notes }) => {
  const [sort, setSort] = useState<Sort>(DEFAULT_SORT);

  const sorted = useMemo(() => {
    const factor = sort.dir === 'asc' ? 1 : -1;
    return [...rows].sort((a, b) => {
      const primary =
        sort.column === 'name'
          ? nameOf(a).localeCompare(nameOf(b))
          : (a[sort.column] as number) - (b[sort.column] as number);
      // The raw key breaks ties so the order never depends on the response's.
      return primary * factor || a.key.localeCompare(b.key);
    });
  }, [rows, sort, nameOf]);

  const onSort = (column: Column, numeric: boolean) => {
    setSort((prev) =>
      prev.column === column
        ? { column, dir: prev.dir === 'asc' ? 'desc' : 'asc' }
        : { column, dir: numeric ? 'desc' : 'asc' },
    );
  };

  if (rows.length === 0) {
    return <div className="settings-hint">No model calls in this range.</div>;
  }

  return (
    <div className="usage-table-wrap">
      <table className="usage-table" id="costsBreakdown">
        <thead>
          <tr>
            {COLUMNS.map((c) => {
              const activeSort = sort.column === c.id;
              return (
                <th
                  key={c.id}
                  scope="col"
                  className={c.numeric ? 'num' : undefined}
                  aria-sort={activeSort ? (sort.dir === 'asc' ? 'ascending' : 'descending') : 'none'}
                >
                  <button type="button" className="usage-sort" onClick={() => onSort(c.id, c.numeric)}>
                    {c.id === 'name' ? SPLIT_NOUN[split] : c.label}
                    <span className="usage-sort-mark" aria-hidden="true">
                      {activeSort ? (sort.dir === 'asc' ? '▲' : '▼') : ''}
                    </span>
                  </button>
                </th>
              );
            })}
          </tr>
        </thead>
        <tbody>
          {sorted.map((row) => {
            const est = row.estimated_calls > 0;
            const estTitle = est
              ? `${fmtCount(row.estimated_calls)} of ${fmtCount(row.calls)} calls have estimated token counts (characters / 4 of the payload)`
              : undefined;
            const estClass = `num${est ? ' usage-est' : ''}`;
            const tokensTitle = (n: number) =>
              est ? `${fmtCount(n)} tokens. ${estTitle}` : `${fmtCount(n)} tokens`;
            const noteNumber = row.note ? notes.indexOf(row.note) + 1 : 0;
            return (
              <tr key={row.key} data-key={row.key}>
                <th
                  scope="row"
                  className="usage-name"
                  aria-describedby={noteNumber > 0 ? noteId(noteNumber) : undefined}
                >
                  <span className="usage-name-line">
                    <span
                      className={`usage-swatch ${seriesClass(slotOf(row.key, series))}`}
                      aria-hidden="true"
                    />
                    <span className="usage-name-text" title={row.key}>
                      {nameOf(row)}
                    </span>
                    {noteNumber > 0 && (
                      <sup className="usage-note-ref" title={row.note}>
                        {noteNumber}
                      </sup>
                    )}
                  </span>
                  {row.deleted && split === 'session' && (
                    <span className="usage-name-key">{row.key}</span>
                  )}
                  {est && (
                    <span className="usage-badge" title={estTitle}>
                      {fmtCount(row.estimated_calls)} estimated
                    </span>
                  )}
                </th>
                <td className={estClass} title={estTitle}>
                  {fmtMoney(row.cost_usd)}
                </td>
                <td className={estClass} title={estTitle}>
                  {fmtFraction(row.share)}
                </td>
                <td className={estClass} title={tokensTitle(row.prompt_tokens)}>
                  {formatTokenCount(row.prompt_tokens)}
                </td>
                <td className={estClass} title={estTitle}>
                  {row.cached_pct.toFixed(1)}%
                </td>
                <td className={estClass} title={tokensTitle(row.output_tokens)}>
                  {formatTokenCount(row.output_tokens)}
                </td>
                <td className="num">{fmtCount(row.calls)}</td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
};
