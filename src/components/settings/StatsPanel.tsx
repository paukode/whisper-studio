import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { useApi } from '@/hooks/useApi';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';

interface CostSummary {
  total_cost_usd?: number;
  session_cost_usd?: number;
  today_total_usd?: number;
  total_turns?: number;
  total_input_tokens?: number;
  total_output_tokens?: number;
  context_used?: number;
  context_max?: number;
  cache?: {
    read_tokens?: number;
    write_tokens?: number;
    hit_rate?: number;
    est_savings_usd?: number;
  };
}

interface LspStatus {
  running?: boolean;
  language?: string;
  pid?: number;
  [key: string]: unknown;
}

export const StatsPanel: React.FC = () => {
  const api = useApi();
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const toolPoolStats = useUIStore((s) => s.toolPoolStats);

  // Data loads via react-query (no setState-in-effect). Per-endpoint error
  // messages are derived from each query's isError; the LSP "Refresh" button
  // refetches its query. Passing the active session id populates
  // context_used/context_max from the loop's real token counts, which is
  // what brings the context bar below to life.
  const statsQuery = useQuery({
    queryKey: ['costs-summary', currentSessionId],
    queryFn: () =>
      api.get<CostSummary>(
        currentSessionId
          ? `/api/costs/summary?session_id=${encodeURIComponent(currentSessionId)}`
          : '/api/costs/summary',
      ),
    staleTime: 30_000,
  });
  const lspQuery = useQuery({
    queryKey: ['lsp-status'],
    queryFn: () => api.get<LspStatus>('/api/lsp/status'),
    staleTime: 30_000,
  });

  const stats = statsQuery.data ?? null;
  const error = statsQuery.isError ? 'No data yet. Send a message to start tracking.' : null;
  const lspStatus = lspQuery.data ?? null;
  const lspError = lspQuery.isError ? 'Could not load LSP status.' : null;

  const fmtCost = (n: number | undefined) => {
    if (n === undefined || n === null) return '-';
    return `$${n.toFixed(4)}`;
  };

  const fmt = (n: number | undefined) => {
    if (n === undefined || n === null) return '-';
    return n.toLocaleString();
  };

  const contextUsed = stats?.context_used ?? 0;
  const contextMax = stats?.context_max ?? 0;
  const contextPct = contextMax > 0 ? Math.min(100, (contextUsed / contextMax) * 100) : 0;
  const showContextBar = contextMax > 0;

  return (
    <div className="settings-form" style={{ maxWidth: 560 }}>
      <h3 style={{ marginBottom: 12 }}>Token Usage &amp; Cost</h3>

      <div id="statsContent">
        {error && <div className="settings-hint">{error}</div>}

        {stats && !error && (
          <div className="settings-list">
            <div className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">Total Cost</div>
                <div className="settings-item-desc">{fmtCost(stats.total_cost_usd)}</div>
              </div>
            </div>
            <div className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">Session Cost</div>
                <div className="settings-item-desc">{fmtCost(stats.session_cost_usd)}</div>
              </div>
            </div>
            <div className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">Today&apos;s Cost</div>
                <div className="settings-item-desc">{fmtCost(stats.today_total_usd)}</div>
              </div>
            </div>
            <div className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">Total Turns</div>
                <div className="settings-item-desc">{fmt(stats.total_turns)}</div>
              </div>
            </div>
            {stats.total_input_tokens !== undefined && (
              <div className="settings-item">
                <div className="settings-item-info">
                  <div className="settings-item-name">Input Tokens</div>
                  <div className="settings-item-desc">{fmt(stats.total_input_tokens)}</div>
                </div>
              </div>
            )}
            {stats.total_output_tokens !== undefined && (
              <div className="settings-item">
                <div className="settings-item-info">
                  <div className="settings-item-name">Output Tokens</div>
                  <div className="settings-item-desc">{fmt(stats.total_output_tokens)}</div>
                </div>
              </div>
            )}
            {toolPoolStats && (
              <div className="settings-item" id="toolPoolStatsRow">
                <div className="settings-item-info">
                  <div className="settings-item-name">Tool Schemas</div>
                  <div className="settings-item-desc">
                    {toolPoolStats.advertised}/{toolPoolStats.total} advertised · ~
                    {Math.round(toolPoolStats.deferred_tokens_est / 1000)}K tokens deferred this turn
                  </div>
                </div>
              </div>
            )}
            {stats.cache && (stats.cache.read_tokens ?? 0) + (stats.cache.write_tokens ?? 0) > 0 && (
              <div className="settings-item" id="cacheStatsRow">
                <div className="settings-item-info">
                  <div className="settings-item-name">Prompt Cache</div>
                  <div className="settings-item-desc">
                    {((stats.cache.hit_rate ?? 0) * 100).toFixed(1)}% hit rate · {fmt(stats.cache.read_tokens)}{' '}
                    tokens read · saved ~{fmtCost(stats.cache.est_savings_usd)}
                  </div>
                </div>
              </div>
            )}
          </div>
        )}
      </div>

      {/* Context usage bar */}
      {showContextBar && (
        <div id="contextUsageBar" style={{ marginTop: 12 }}>
          <div className="stat-label" style={{ marginBottom: 4 }}>Context usage</div>
          <div className="context-bar-track">
            <div className="context-bar-fill" id="contextBarFill" style={{ width: `${contextPct}%` }}></div>
          </div>
          <div className="settings-hint" id="contextUsageHint">
            {fmt(contextUsed)} / {fmt(contextMax)} tokens ({contextPct.toFixed(1)}%)
          </div>
        </div>
      )}

      <hr style={{ margin: '16px 0', borderColor: 'var(--border)' }} />
      <h3 style={{ marginBottom: 8 }}>LSP Status</h3>
      {lspError && <div className="settings-hint" id="lspStatusText">{lspError}</div>}
      {lspStatus ? (
        <div className="settings-list">
          {Object.entries(lspStatus).map(([key, value]) => (
            <div key={key} className="settings-item">
              <div className="settings-item-info">
                <div className="settings-item-name">{key}</div>
                <div className="settings-item-desc">{String(value)}</div>
              </div>
            </div>
          ))}
        </div>
      ) : (
        !lspError && (
          <div className="settings-hint" id="lspStatusText" aria-busy="true">
            <span className="skeleton skeleton-text" style={{ width: '50%' }} />
          </div>
        )
      )}
      <button className="btn btn-sm" id="lspRefreshBtn" type="button" onClick={() => void lspQuery.refetch()} style={{ marginTop: 8 }}>
        Refresh
      </button>
    </div>
  );
};
