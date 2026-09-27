import React, { useMemo, useState } from 'react';
import type { UsageBucket, UsageGranularity } from '@/types/costs';
import { bucketSegments, seriesClass, type ChartSeries } from './usageSeries';
import { bucketLabel, fmtCount, fmtMoney } from './usageView';

interface UsageChartProps {
  buckets: UsageBucket[];
  series: ChartSeries[];
  granularity: UsageGranularity;
  /** Shown over the plot when the range holds no model calls. */
  emptyMessage?: string;
}

/** The chart's heading and accessible name, per granularity. */
export const CHART_TITLE: Record<UsageGranularity, string> = {
  day: 'Estimated cost per UTC day',
  week: 'Estimated cost per week (UTC, from Monday)',
  month: 'Estimated cost per month (UTC)',
};

/** Past this many bars the gaps between them would outgrow the plot. */
const DENSE_BUCKETS = 60;

/**
 * One bar per bucket, stacked by the top split keys plus "Other". The server
 * zero-fills the range, so a quiet day is a visible gap, not a missing bar. A
 * week or month clipped by the range is drawn lighter and named by its dates.
 * Hover (or focus the plot and use the arrow keys) for a bucket's figures; a
 * series with estimated token counts in the range is marked as such there.
 */
export const UsageChart: React.FC<UsageChartProps> = ({ buckets, series, granularity, emptyMessage }) => {
  const [active, setActive] = useState<number | null>(null);

  const ordered = useMemo(
    () => [...buckets].sort((a, b) => a.start.localeCompare(b.start)),
    [buckets],
  );
  const stacks = useMemo(() => ordered.map((b) => bucketSegments(b, series)), [ordered, series]);
  const max = ordered.reduce((m, b) => Math.max(m, b.cost_usd), 0);
  const n = ordered.length;
  const current = active !== null && active < n ? active : null;

  const onKeyDown = (e: React.KeyboardEvent) => {
    if (n === 0) return;
    let next: number | null = current;
    if (e.key === 'ArrowRight') next = current === null ? 0 : Math.min(n - 1, current + 1);
    else if (e.key === 'ArrowLeft') next = current === null ? n - 1 : Math.max(0, current - 1);
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = n - 1;
    else if (e.key === 'Escape') next = null;
    else return;
    e.preventDefault();
    setActive(next);
  };

  const tooltipAlign =
    current === null ? '' : current < n / 3 ? ' align-start' : current > (2 * n) / 3 ? ' align-end' : '';

  return (
    <div className="usage-chart">
      {/* Ticks sit on the plot's gridlines (top, middle, base); the hidden
          sizer holds the column as wide as the longest of them. */}
      <div className="usage-chart-axis-y" aria-hidden="true">
        <span className="usage-chart-axis-sizer">{fmtMoney(max)}</span>
        <span className="usage-chart-tick top">{fmtMoney(max)}</span>
        {max > 0 && <span className="usage-chart-tick mid">{fmtMoney(max / 2)}</span>}
        <span className="usage-chart-tick base">{fmtMoney(0)}</span>
      </div>
      <div
        className="usage-chart-plot"
        role="group"
        aria-label={CHART_TITLE[granularity]}
        tabIndex={0}
        onKeyDown={onKeyDown}
        onMouseLeave={() => setActive(null)}
        onBlur={() => setActive(null)}
      >
        <div className={`usage-chart-bars${n > DENSE_BUCKETS ? ' dense' : ''}`}>
          {ordered.map((b, i) => {
            const label = bucketLabel(b, granularity);
            const height = max > 0 ? (b.cost_usd / max) * 100 : 0;
            return (
              <div
                key={b.start}
                className={`usage-chart-col${b.partial ? ' partial' : ''}${current === i ? ' active' : ''}`}
                role="img"
                aria-label={`${label}: ${fmtMoney(b.cost_usd)}`}
                data-bucket={b.start}
                onMouseEnter={() => setActive(i)}
              >
                <div
                  className={`usage-chart-bar${b.cost_usd > 0 ? ' nonzero' : ''}`}
                  style={{ height: `${height}%` }}
                >
                  {b.cost_usd > 0 &&
                    stacks[i].map((seg, j) =>
                      seg.cost_usd > 0 ? (
                        <div
                          key={seg.key}
                          className={`usage-chart-seg ${seriesClass(series[j].slot)}`}
                          style={{ height: `${(seg.cost_usd / b.cost_usd) * 100}%` }}
                        />
                      ) : null,
                    )}
                </div>
              </div>
            );
          })}
        </div>
        {emptyMessage && <div className="usage-chart-empty">{emptyMessage}</div>}
        {current !== null && (
          <div
            className={`usage-chart-tip${tooltipAlign}`}
            role="status"
            style={{ left: `${((current + 0.5) / n) * 100}%` }}
          >
            <div className="usage-chart-tip-title">
              {bucketLabel(ordered[current], granularity)}: {fmtMoney(ordered[current].cost_usd)}
            </div>
            {stacks[current].some((s) => s.calls > 0) ? (
              <ul className="usage-chart-tip-list">
                {stacks[current].map((seg, j) =>
                  seg.calls > 0 ? (
                    <li key={seg.key} className={series[j].estimated ? 'usage-est' : undefined}>
                      <span className={`usage-swatch ${seriesClass(series[j].slot)}`} aria-hidden="true" />
                      <span className="usage-chart-tip-name">{series[j].label}</span>{' '}
                      {fmtMoney(seg.cost_usd)} · {fmtCount(seg.prompt_tokens)} prompt ·{' '}
                      {fmtCount(seg.output_tokens)} output · {fmtCount(seg.calls)} calls
                      {series[j].estimated && ' · may include estimated counts'}
                    </li>
                  ) : null,
                )}
              </ul>
            ) : (
              <div className="usage-chart-tip-empty">No model calls</div>
            )}
          </div>
        )}
      </div>
      <div className="usage-chart-axis-x" aria-hidden="true">
        {n > 0 && <span>{bucketLabel(ordered[0], granularity)}</span>}
        {n > 1 && <span>{bucketLabel(ordered[n - 1], granularity)}</span>}
      </div>
      {series.length > 0 && (
        <ul className="usage-legend" aria-label="Chart colours">
          {series.map((s) => (
            <li key={s.key}>
              <span className={`usage-swatch ${seriesClass(s.slot)}`} aria-hidden="true" />
              {s.label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
};
