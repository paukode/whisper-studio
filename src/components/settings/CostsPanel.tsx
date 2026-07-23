import React, { useCallback, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put, post } from '@/api/client';

interface CostSummary {
  total_cost_usd?: number;
  session_cost_usd?: number;
  today_total_usd?: number;
  total_turns?: number;
}

interface ModelCost {
  model: string;
  input_tokens: number;
  output_tokens: number;
  cost_usd: number;
}

interface DailyCost {
  date: string;
  cost_usd: number;
}

interface BudgetConfig {
  max_session_cost_usd?: number;
  max_daily_cost_usd?: number;
  model_fallback_enabled?: boolean;
}

export const CostsPanel: React.FC = () => {
  const queryClient = useQueryClient();

  // Cost data loads via react-query (no setState-in-effect). Three independent
  // queries mirror the three endpoints the old loadData() hit.
  const summaryQuery = useQuery({
    queryKey: ['costs-summary'],
    queryFn: () => get<CostSummary>('/api/costs/summary'),
    staleTime: 30_000,
  });
  const modelsQuery = useQuery({
    queryKey: ['costs-models'],
    queryFn: () => get<{ models: ModelCost[] }>('/api/costs/models'),
    staleTime: 30_000,
  });
  const dailyQuery = useQuery({
    queryKey: ['costs-daily'],
    queryFn: () => get<{ daily: DailyCost[] }>('/api/costs/daily'),
    staleTime: 30_000,
  });

  const summary = summaryQuery.data ?? null;
  const error = summaryQuery.isError ? 'Loading cost data...' : null;
  const models = modelsQuery.data?.models ?? [];
  const dailyCosts = dailyQuery.data?.daily ?? [];

  // Budget settings
  const [maxSessionCost, setMaxSessionCost] = useState('');
  const [maxDailyCost, setMaxDailyCost] = useState('');
  const [modelFallback, setModelFallback] = useState(false);
  const [budgetHint, setBudgetHint] = useState('');

  // Seed the budget fields from the live config so an existing cap is visible
  // and a save doesn't clobber an unshown value. Uses the real backend keys
  // (see server/infrastructure/config.py DEFAULTS). If the fetch fails the
  // fields stay blank/default.
  const configQuery = useQuery({
    queryKey: ['config'],
    queryFn: () => get<BudgetConfig>('/api/config'),
    staleTime: 30_000,
  });
  // Seed during render via the previous-value pattern rather than an effect
  // (React Compiler flags setState-in-effect). Only fires when the query data
  // identity changes, so it doesn't loop or clobber edits on every render.
  const [seededFrom, setSeededFrom] = useState<BudgetConfig | undefined>(undefined);
  if (configQuery.data && configQuery.data !== seededFrom) {
    const cfg = configQuery.data;
    setSeededFrom(cfg);
    // 0 means "no limit" — show it as blank so the placeholder hint applies.
    const session = cfg.max_session_cost_usd;
    const daily = cfg.max_daily_cost_usd;
    setMaxSessionCost(typeof session === 'number' && session > 0 ? String(session) : '');
    setMaxDailyCost(typeof daily === 'number' && daily > 0 ? String(daily) : '');
    setModelFallback(!!cfg.model_fallback_enabled);
  }

  const fmtCost = (n: number | undefined) => {
    if (n === undefined || n === null) return '-';
    return `$${n.toFixed(4)}`;
  };

  const handleSaveBudget = useCallback(async () => {
    setBudgetHint('');
    try {
      // Use the real backend config keys (server/infrastructure/config.py
      // DEFAULTS). update_config only persists keys present in DEFAULTS, so the
      // old names (max_session_cost / max_daily_cost / model_fallback) were
      // silently dropped and budget enforcement never saw them.
      const body: Record<string, unknown> = {};
      if (maxSessionCost) body.max_session_cost_usd = parseFloat(maxSessionCost);
      if (maxDailyCost) body.max_daily_cost_usd = parseFloat(maxDailyCost);
      body.model_fallback_enabled = modelFallback;
      await put('/api/config', body);
      setBudgetHint('Saved!');
      setTimeout(() => setBudgetHint(''), 3000);
    } catch {
      setBudgetHint('Save failed');
    }
  }, [maxSessionCost, maxDailyCost, modelFallback]);

  const handleResetDaily = useCallback(async () => {
    try {
      await post('/api/costs/reset-daily');
      setBudgetHint('Daily counter reset');
      setTimeout(() => setBudgetHint(''), 3000);
      void queryClient.invalidateQueries({ queryKey: ['costs-summary'] });
      void queryClient.invalidateQueries({ queryKey: ['costs-models'] });
      void queryClient.invalidateQueries({ queryKey: ['costs-daily'] });
    } catch {
      setBudgetHint('Reset failed');
    }
  }, [queryClient]);

  const handleExport = useCallback((format: 'csv' | 'json') => {
    const url = `/api/costs/export?format=${format}`;
    const a = document.createElement('a');
    a.href = url;
    a.download = `costs-export.${format}`;
    a.click();
  }, []);

  // Compute max daily cost for bar chart scaling
  const maxDailyValue = dailyCosts.reduce((max, d) => Math.max(max, d.cost_usd), 0) || 1;

  return (
    <div className="settings-form" style={{ maxWidth: 600 }}>
      <h3 style={{ marginBottom: 12 }}>Cost Dashboard</h3>

      <div id="costsDashboard">
        {error && !summary && <div className="settings-hint">{error}</div>}
        {summary && (
          <div className="settings-list">
            <div className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">Total Cost</div>
                <div className="settings-item-desc">{fmtCost(summary.total_cost_usd)}</div>
              </div>
            </div>
            <div className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">Today&apos;s Cost</div>
                <div className="settings-item-desc">{fmtCost(summary.today_total_usd)}</div>
              </div>
            </div>
            <div className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">Session Cost</div>
                <div className="settings-item-desc">{fmtCost(summary.session_cost_usd)}</div>
              </div>
            </div>
          </div>
        )}
      </div>

      <hr style={{ margin: '16px 0', borderColor: 'var(--border)' }} />
      <h3 style={{ marginBottom: 8 }}>Model Breakdown</h3>
      <div id="costsModelBreakdown">
        {models.length > 0 ? (
          <div className="settings-list">
            {models.map((m) => (
              <div key={m.model} className="settings-item">
                <div className="settings-item-info">
                  <div className="settings-item-name">{m.model}</div>
                  <div className="settings-item-desc">
                    In: {m.input_tokens.toLocaleString()} · Out: {m.output_tokens.toLocaleString()} · {fmtCost(m.cost_usd)}
                  </div>
                </div>
              </div>
            ))}
          </div>
        ) : (
          <div className="settings-hint">No model data yet.</div>
        )}
      </div>

      <hr style={{ margin: '16px 0', borderColor: 'var(--border)' }} />
      <h3 style={{ marginBottom: 8 }}>Daily Costs</h3>
      <div id="costsDailyChart" className="cost-bar-chart">
        {dailyCosts.length > 0 ? (
          dailyCosts.map((d) => (
            <div key={d.date} className="cost-bar-row" style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 4 }}>
              <span style={{ fontSize: '0.8em', minWidth: 70, color: 'var(--text-muted)' }}>{d.date}</span>
              <div style={{ flex: 1, background: 'var(--bg-secondary)', borderRadius: 3, height: 16, overflow: 'hidden' }}>
                <div style={{
                  width: `${(d.cost_usd / maxDailyValue) * 100}%`,
                  height: '100%',
                  background: 'var(--accent)',
                  borderRadius: 3,
                  minWidth: d.cost_usd > 0 ? 2 : 0,
                }}></div>
              </div>
              <span style={{ fontSize: '0.8em', minWidth: 60, textAlign: 'right' }}>{fmtCost(d.cost_usd)}</span>
            </div>
          ))
        ) : (
          <div className="settings-hint">No daily data yet.</div>
        )}
      </div>

      <hr style={{ margin: '16px 0', borderColor: 'var(--border)' }} />
      <h3 style={{ marginBottom: 12 }}>Budget Settings</h3>
      <div className="settings-form">
        <label>Max Session Cost (USD)</label>
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
        <label>Max Daily Cost (USD)</label>
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
        <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 8 }}>
          <input
            type="checkbox"
            id="budgetModelFallback"
            checked={modelFallback}
            onChange={(e) => setModelFallback(e.target.checked)}
          />
          Enable model fallback (downgrade model when approaching budget)
        </label>
        <div style={{ marginTop: 12, display: 'flex', gap: 8 }}>
          <button className="btn btn-primary btn-sm" id="budgetSaveBtn" type="button" onClick={() => void handleSaveBudget()}>
            Save Budget
          </button>
          <button className="btn btn-sm" id="budgetResetDailyBtn" type="button" style={{ opacity: 0.7 }} onClick={() => void handleResetDaily()}>
            Reset Daily Counter
          </button>
        </div>
        <div className="settings-hint" id="budgetSaveHint" style={{ marginTop: 8 }}>{budgetHint}</div>
      </div>

      <hr style={{ margin: '16px 0', borderColor: 'var(--border)' }} />
      <h3 style={{ marginBottom: 8 }}>Export</h3>
      <div style={{ display: 'flex', gap: 8 }}>
        <button className="btn btn-sm" id="costsExportCSV" type="button" onClick={() => handleExport('csv')}>
          Export CSV
        </button>
        <button className="btn btn-sm" id="costsExportJSON" type="button" onClick={() => handleExport('json')}>
          Export JSON
        </button>
      </div>
    </div>
  );
};
