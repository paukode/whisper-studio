import React from 'react';
import type { UsageGranularity, UsageSplit } from '@/types/costs';
import {
  GRANULARITY_OPTIONS,
  RANGE_OPTIONS,
  SPLIT_OPTIONS,
  type CostsView,
  type RangePreset,
} from './usageView';

interface SegmentedProps<T extends string> {
  label: string;
  /** Keep the label for assistive tech only, when the options name themselves. */
  hideLabel?: boolean;
  id: string;
  value: T;
  options: ReadonlyArray<{ value: T; label: string }>;
  onChange: (value: T) => void;
}

/** A row of mutually exclusive buttons; the pressed one is the current value. */
function Segmented<T extends string>({
  label,
  hideLabel,
  id,
  value,
  options,
  onChange,
}: SegmentedProps<T>) {
  return (
    <div className="usage-control">
      <span className={hideLabel ? 'sr-only' : 'usage-control-label'} id={`${id}Label`}>
        {label}
      </span>
      <div className="usage-segmented" role="group" aria-labelledby={`${id}Label`} id={id}>
        {options.map((o) => (
          <button
            key={o.value}
            type="button"
            className={`usage-segment${o.value === value ? ' active' : ''}`}
            aria-pressed={o.value === value}
            onClick={() => onChange(o.value)}
          >
            {o.label}
          </button>
        ))}
      </div>
    </div>
  );
}

interface UsageControlsProps {
  view: CostsView;
  onRange: (range: RangePreset) => void;
  onGranularity: (granularity: UsageGranularity) => void;
  onSplit: (split: UsageSplit) => void;
}

/** Range, granularity and split: the controls that drive every section of the
 *  tab below them, on one line that stays in view while the report scrolls.
 *  The range and Day / Week / Month read for themselves, so only the split
 *  shows its label; every control keeps its accessible name. A custom range's
 *  dates sit on the line below ({@link UsageCustomRange}), so choosing Custom
 *  never pushes the other controls onto a second row. */
export const UsageControls: React.FC<UsageControlsProps> = ({
  view,
  onRange,
  onGranularity,
  onSplit,
}) => (
  <div className="usage-toolbar" id="costsToolbar">
    <div className="usage-control">
      <label className="sr-only" htmlFor="costsRange">
        Range
      </label>
      <select
        id="costsRange"
        className="usage-field"
        value={view.range}
        onChange={(e) => onRange(e.target.value as RangePreset)}
      >
        {RANGE_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.label}
          </option>
        ))}
      </select>
    </div>
    <Segmented
      label="Granularity"
      hideLabel
      id="costsGranularity"
      value={view.granularity}
      options={GRANULARITY_OPTIONS}
      onChange={onGranularity}
    />
    <Segmented
      label="Split by"
      id="costsSplit"
      value={view.split}
      options={SPLIT_OPTIONS}
      onChange={onSplit}
    />
  </div>
);

interface UsageCustomRangeProps {
  view: CostsView;
  /** Today's UTC day, the latest date the pickers offer. */
  today: string;
  granularity: UsageGranularity;
  onCustom: (edge: 'customFrom' | 'customTo', value: string) => void;
}

/** A custom range's two dates, on the line where a preset's dates read. It
 *  stays up while the range is invalid, since it is how that gets fixed. */
export const UsageCustomRange: React.FC<UsageCustomRangeProps> = ({
  view,
  today,
  granularity,
  onCustom,
}) => (
  <div className="usage-range-line usage-custom-range">
    <label className="sr-only" htmlFor="costsFrom">
      From (UTC)
    </label>
    <input
      id="costsFrom"
      type="date"
      className="usage-field usage-date"
      value={view.customFrom}
      max={today}
      onChange={(e) => onCustom('customFrom', e.target.value)}
    />
    <span aria-hidden="true">to</span>
    <label className="sr-only" htmlFor="costsTo">
      To (UTC)
    </label>
    <input
      id="costsTo"
      type="date"
      className="usage-field usage-date"
      value={view.customTo}
      max={today}
      onChange={(e) => onCustom('customTo', e.target.value)}
    />
    <span>
      UTC days{granularity === 'week' && ', weeks start on Monday'}
    </span>
  </div>
);
