import React, { useCallback, useMemo, useState } from 'react';
import { keepPreviousData, useQuery } from '@tanstack/react-query';
import { downloadUrl } from '@/utils/downloadFile';
import { useSettingsStore } from '@/stores/settingsStore';
import type { UsageGranularity, UsageRow, UsageSplit } from '@/types/costs';
import { UsageControls, UsageCustomRange } from './costs/UsageControls';
import { UsageTiles } from './costs/UsageTiles';
import { CHART_TITLE, UsageChart } from './costs/UsageChart';
import { UsageTable } from './costs/UsageTable';
import { BudgetSection } from './costs/BudgetSection';
import { fetchUsage, usageQueryKey } from './costs/usageApi';
import { useUtcNow } from './costs/useUtcNow';
import { chartSeries, noteId, rowName } from './costs/usageSeries';
import {
  ALL_TIME_FROM,
  exportUrl,
  fmtCount,
  fmtDay,
  fmtRange,
  loadCostsView,
  presetRange,
  resolveRange,
  saveCostsView,
  utcToday,
  type CostsView,
  type RangePreset,
} from './costs/usageView';

const errorText = (err: unknown): string => (err instanceof Error ? err.message : String(err));

/**
 * Settings > Usage and advanced > Costs: usage and estimated cost over a
 * chosen range of UTC days, bucketed by day, week or month and split by
 * model, session or source. One report (GET /api/costs/usage) feeds the
 * tiles, the chart and the table, so no figure is shown twice from two
 * sources. The composer's TokenCounter stays the only per-session readout.
 */
export const CostsPanel: React.FC = () => {
  const pickerModels = useSettingsStore((sel) => sel.models);
  const pickerName = useCallback(
    (key: string) => pickerModels.find((pm) => pm.key === key)?.name,
    [pickerModels],
  );

  const [view, setView] = useState<CostsView>(loadCostsView);
  // One moment per UTC day for every preset and the budget hint, so they
  // cannot straddle a midnight mid-render; it moves on at 00:00 UTC.
  const now = useUtcNow();
  const today = utcToday(now);
  const resolved = resolveRange(view, now);
  const range = resolved.ok ? resolved.range : null;

  const update = (patch: Partial<CostsView>) => {
    const next = { ...view, ...patch };
    setView(next);
    saveCostsView(next);
  };

  const onRange = (preset: RangePreset) => {
    if (preset === 'custom' && (!view.customFrom || !view.customTo)) {
      // Start the custom pickers on the range already on screen.
      const seed = range ?? presetRange('last30', now);
      update({ range: preset, customFrom: seed.from, customTo: seed.to });
      return;
    }
    update({ range: preset });
  };

  const usageQuery = useQuery({
    queryKey: range
      ? usageQueryKey(range, view.granularity, view.split)
      : ['costs-usage', 'no-range'],
    queryFn: () => fetchUsage(range!, view.granularity, view.split),
    enabled: range !== null,
    staleTime: 30_000,
    // Keep the last report on screen while the next one loads. It renders with
    // its own granularity and split, so the labels always match the figures.
    placeholderData: keepPreviousData,
  });

  // Today's UTC spend for the daily-cap hint: the same report, one day wide.
  const todayRange = { from: today, to: today };
  const todayQuery = useQuery({
    queryKey: usageQueryKey(todayRange, 'day', 'model'),
    queryFn: () => fetchUsage(todayRange, 'day', 'model'),
    staleTime: 30_000,
  });

  const data = usageQuery.data;
  const shownSplit: UsageSplit = data?.split ?? view.split;
  const shownGranularity: UsageGranularity = data?.granularity ?? view.granularity;
  const nameOf = useCallback(
    (row: UsageRow) => rowName(row, shownSplit, pickerName),
    [shownSplit, pickerName],
  );
  const series = useMemo(
    () => (data ? chartSeries(data.rows, data.buckets, nameOf) : []),
    [data, nameOf],
  );
  const notes = useMemo(
    () => (data ? [...new Set(data.rows.map((r) => r.note).filter(Boolean))] : []),
    [data],
  );

  const handleExport = (format: 'csv' | 'json') => {
    if (!range) return;
    downloadUrl(exportUrl(range, format), `costs-${range.from}-to-${range.to}.${format}`);
  };
  const exportHint = range
    ? `Exports ${fmtRange(range.from, range.to)} (UTC) with each row's count source, priced at export time.`
    : 'Pick a valid range to export.';

  const stale = usageQuery.isPlaceholderData;
  // The chart plots cost, so a range of calls that all cost nothing (Local
  // mode, the first-run default) would draw flat bars with no explanation. A
  // billed call with no rate in the table also shows as $0, but it is not
  // free, so it is never described as an on-device call.
  const chartEmpty = !data
    ? undefined
    : data.totals.calls === 0
      ? 'No model calls in this range.'
      : data.totals.cost_usd !== 0
        ? undefined
        : data.totals.unpriced_calls === 0
          ? 'Every call in this range cost $0.00, as on-device calls do. The breakdown below lists their tokens.'
          : data.totals.unpriced_calls === data.totals.calls
            ? 'Every call in this range is on a model with no rate in the pricing table, so its spend shows as $0 and is not counted. The breakdown below lists their tokens.'
            : `${fmtCount(data.totals.unpriced_calls)} of ${fmtCount(data.totals.calls)} calls in this range are on models with no rate in the pricing table, so their spend shows as $0 and is not counted; the others cost $0.00. The breakdown below lists their tokens.`;

  return (
    <div className="settings-form usage-panel">
      <div className="usage-header">
        <div className="usage-heading">
          <h3 className="usage-title">Usage and cost</h3>
          <p className="settings-hint usage-intro">
            Estimated from provider token counts at the rates in pricing.json. Your AWS invoice can
            differ.
          </p>
        </div>
        <div className="usage-export">
          <button
            className="btn btn-sm"
            id="costsExportCSV"
            type="button"
            disabled={!range}
            title={exportHint}
            onClick={() => handleExport('csv')}
          >
            Export CSV
          </button>
          <button
            className="btn btn-sm"
            id="costsExportJSON"
            type="button"
            disabled={!range}
            title={exportHint}
            onClick={() => handleExport('json')}
          >
            Export JSON
          </button>
        </div>
      </div>

      <UsageControls
        view={view}
        onRange={onRange}
        onGranularity={(granularity) => update({ granularity })}
        onSplit={(split) => update({ split })}
      />
      {view.range === 'custom' && (
        <UsageCustomRange
          view={view}
          today={today}
          granularity={shownGranularity}
          onCustom={(edge, value) =>
            update(edge === 'customFrom' ? { customFrom: value } : { customTo: value })
          }
        />
      )}

      {!resolved.ok && (
        <div className="settings-hint settings-hint--warn" role="alert" id="costsRangeError">
          {resolved.reason}
        </div>
      )}
      {usageQuery.isError && (
        <div className="settings-hint settings-hint--warn" role="alert" id="costsUsageError">
          Could not load usage: {errorText(usageQuery.error)}
        </div>
      )}
      {range && usageQuery.isPending && (
        <div className="settings-hint" aria-busy="true">
          Loading usage
        </div>
      )}

      {range && data && (
        <div className={`usage-report${stale ? ' stale' : ''}`} aria-busy={stale}>
          {/* A custom range's dates are its pickers, above. */}
          {view.range !== 'custom' && (
            <div className="usage-range-line" id="costsRangeLine">
              {fmtRange(data.range.from, data.range.to)}, UTC days
              {shownGranularity === 'week' && ', weeks start on Monday'}
            </div>
          )}
          {view.range === 'all' && data.range.from === ALL_TIME_FROM && (
            <p className="settings-hint" id="costsAllTimeNote">
              All time counts from {fmtDay(ALL_TIME_FROM, true)}, the app's first public release.
              Spend recorded before that day is not included.
            </p>
          )}

          <UsageTiles totals={data.totals} />

          {/* The fine print for the figures above and the table below: each
              pricing note once, numbered so a table row can point at it. */}
          {notes.length > 0 && (
            <ol className="usage-notes" id="costsNotes" aria-label="Pricing notes">
              {notes.map((n, i) => (
                <li key={n} id={noteId(i + 1)}>
                  <span className="usage-note-mark" aria-hidden="true">
                    {i + 1}
                  </span>
                  {n}
                </li>
              ))}
            </ol>
          )}
          {data.totals.estimated_calls > 0 && (
            <p className="settings-hint usage-est-legend" id="costsEstLegend">
              Figures in italics include calls whose token counts the provider did not report. Those
              counts are estimated as characters / 4 of the payload sent and received.
            </p>
          )}

          <div className="usage-card">
            <h4 className="usage-section-title">{CHART_TITLE[shownGranularity]}</h4>
            <UsageChart
              buckets={data.buckets}
              series={series}
              granularity={shownGranularity}
              emptyMessage={chartEmpty}
            />
          </div>

          <div className="usage-card usage-card--table">
            <h4 className="usage-section-title">Breakdown</h4>
            <UsageTable
              rows={data.rows}
              split={shownSplit}
              nameOf={nameOf}
              series={series}
              notes={notes}
            />
          </div>
        </div>
      )}

      <div className="usage-card">
        <h4 className="usage-section-title">Budgets</h4>
        <BudgetSection
          today={{
            cost: todayQuery.data?.totals.cost_usd ?? null,
            calls: todayQuery.data?.totals.calls ?? 0,
            estimatedCalls: todayQuery.data?.totals.estimated_calls ?? 0,
            error: todayQuery.isError ? errorText(todayQuery.error) : null,
          }}
        />
      </div>
    </div>
  );
};
