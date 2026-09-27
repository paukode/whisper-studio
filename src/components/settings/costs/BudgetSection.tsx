import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { fmtCount, fmtMoney } from './usageView';

interface BudgetConfig {
  max_session_cost_usd?: number;
  max_daily_cost_usd?: number;
  model_fallback_enabled?: boolean;
}

/** Spend so far on today's UTC day, or why it is unknown. */
export interface TodaySpend {
  cost: number | null;
  /** Calls today, and how many of them had estimated token counts. */
  calls: number;
  estimatedCalls: number;
  error: string | null;
}

interface BudgetSectionProps {
  today: TodaySpend;
}

/**
 * The spend caps. The daily cap counts the UTC day (the same day Cost
 * Explorer uses) and resets on its own at 00:00 UTC, so there is no reset
 * control: the cost rows are the history, and nothing here deletes them.
 */
export const BudgetSection: React.FC<BudgetSectionProps> = ({ today }) => {
  const [maxSessionCost, setMaxSessionCost] = useState('');
  const [maxDailyCost, setMaxDailyCost] = useState('');
  const [modelFallback, setModelFallback] = useState(false);
  const [budgetHint, setBudgetHint] = useState('');

  // Seed the fields from the live config so an existing cap is visible and a
  // save does not clobber an unshown value. The keys are the backend's own
  // (server/infrastructure/config.py DEFAULTS). If the fetch fails the fields
  // stay blank.
  const configQuery = useQuery({
    queryKey: ['config'],
    queryFn: () => get<BudgetConfig>('/api/config'),
    staleTime: 30_000,
  });
  // Seed during render via the previous-value pattern rather than an effect
  // (React Compiler flags setState-in-effect). Only fires when the query data
  // identity changes, so it does not loop or clobber edits on every render.
  const [seededFrom, setSeededFrom] = useState<BudgetConfig | undefined>(undefined);
  if (configQuery.data && configQuery.data !== seededFrom) {
    const cfg = configQuery.data;
    setSeededFrom(cfg);
    // 0 means "no limit": show it blank so the placeholder hint applies.
    const session = cfg.max_session_cost_usd;
    const daily = cfg.max_daily_cost_usd;
    setMaxSessionCost(typeof session === 'number' && session > 0 ? String(session) : '');
    setMaxDailyCost(typeof daily === 'number' && daily > 0 ? String(daily) : '');
    setModelFallback(!!cfg.model_fallback_enabled);
  }

  const handleSaveBudget = useCallback(async () => {
    setBudgetHint('');
    try {
      // Always send both cost keys, even when blank: update_config only
      // overwrites keys present in the body, so omitting a cleared field left
      // the old limit on disk forever. 0 is the existing "unlimited" value.
      const body: Record<string, unknown> = {
        max_session_cost_usd: maxSessionCost ? parseFloat(maxSessionCost) : 0,
        max_daily_cost_usd: maxDailyCost ? parseFloat(maxDailyCost) : 0,
        model_fallback_enabled: modelFallback,
      };
      await put('/api/config', body);
      setBudgetHint('Saved!');
      setTimeout(() => setBudgetHint(''), 3000);
    } catch {
      setBudgetHint('Save failed');
    }
  }, [maxSessionCost, maxDailyCost, modelFallback]);

  const todayText =
    today.error !== null
      ? `Today so far (UTC day): unavailable (${today.error})`
      : today.cost === null
        ? 'Today so far (UTC day): loading'
        : `Today so far (UTC day): ${fmtMoney(today.cost)}`;
  // A figure resting on characters / 4 estimates is shown as estimated, as
  // the tiles and the table above mark theirs.
  const todayEstimated = today.error === null && today.cost !== null && today.estimatedCalls > 0;

  // The two caps side by side, today's spend under the daily one it counts
  // against, then the fallback switch and the save row.
  return (
    <div className="usage-budget">
      <div className="usage-budget-fields">
        <div className="usage-budget-field">
          <label htmlFor="budgetMaxSession">Max Session Cost (USD)</label>
          <input
            type="number"
            step="0.01"
            min="0"
            className="settings-input"
            id="budgetMaxSession"
            placeholder="e.g. 5.00 (0 = no limit)"
            value={maxSessionCost}
            onChange={(e) => setMaxSessionCost(e.target.value)}
          />
        </div>
        <div className="usage-budget-field">
          <label htmlFor="budgetMaxDaily">Max Daily Cost (USD, UTC day)</label>
          <input
            type="number"
            step="0.01"
            min="0"
            className="settings-input"
            id="budgetMaxDaily"
            placeholder="e.g. 20.00 (0 = no limit)"
            value={maxDailyCost}
            onChange={(e) => setMaxDailyCost(e.target.value)}
          />
          <div
            className={`settings-hint usage-budget-today${todayEstimated ? ' usage-est' : ''}`}
            id="budgetTodaySoFar"
          >
            {todayText}
            {todayEstimated && (
              <span className="usage-est-note">
                {' '}
                (includes {fmtCount(today.estimatedCalls)} of {fmtCount(today.calls)} calls with
                estimated token counts)
              </span>
            )}
          </div>
        </div>
      </div>
      <label className="usage-check">
        <input
          type="checkbox"
          id="budgetModelFallback"
          checked={modelFallback}
          onChange={(e) => setModelFallback(e.target.checked)}
        />
        Enable model fallback (downgrade model when approaching budget)
      </label>
      <div className="usage-budget-actions">
        <button className="btn btn-primary btn-sm" id="budgetSaveBtn" type="button" onClick={() => void handleSaveBudget()}>
          Save Budget
        </button>
        <span className="settings-hint" id="budgetSaveHint">
          {budgetHint}
        </span>
      </div>
    </div>
  );
};
