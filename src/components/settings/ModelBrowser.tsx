import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchRepoDetail,
  installModel,
  searchModels,
  type BrowseResult,
  type BrowseSort,
} from '@/api/modelBrowser';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { humanSize } from '@/utils/humanSize';

/** Compact download count: 4685368 → "4.7M", 308619 → "309K". */
const humanCount = (n: number | null | undefined): string => {
  if (!n || n <= 0) return '0';
  if (n >= 1e6) return `${(n / 1e6).toFixed(1)}M`;
  if (n >= 1e3) return `${Math.round(n / 1e3)}K`;
  return String(n);
};

/** Expandable quant picker for one repo. Fetches the quant list on expand,
 *  pre-selects the recommended quant, and installs the chosen file. */
const RepoRow: React.FC<{ result: BrowseResult }> = ({ result }) => {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [installing, setInstalling] = useState(false);
  const queryClient = useQueryClient();
  const addToast = useUIStore((s) => s.addToast);

  const detailQuery = useQuery({
    queryKey: ['model-browse-repo', result.repo_id],
    queryFn: () => fetchRepoDetail(result.repo_id),
    enabled: open,
  });

  // Pre-select the recommended quant once the detail lands (only if nothing
  // picked yet, so a user's manual pick is never overridden by a refetch).
  const detail = detailQuery.data;
  const effectiveSelected =
    selected ?? detail?.recommended_filename ?? detail?.quants[0]?.filename ?? null;

  const onInstall = async () => {
    if (!effectiveSelected) return;
    setInstalling(true);
    try {
      const res = await installModel({ repo_id: result.repo_id, filename: effectiveSelected });
      addToast({
        type: res.download === 'queued' ? 'info' : 'success',
        message:
          res.download === 'queued'
            ? `${res.label} queued. See the download list below.`
            : res.download === 'already_installed'
              ? `${res.label} is already downloaded and now in your models.`
              : `${res.label} added; downloading. See the download list below.`,
        duration: 6000,
      });
      // Surface the new download row in the manager list above, and refresh the
      // composer picker so it appears there once installed.
      await queryClient.invalidateQueries({ queryKey: ['models-manager-catalog'] });
      await queryClient.invalidateQueries({ queryKey: ['models-manager-status'] });
      await queryClient.invalidateQueries({ queryKey: ['models-disabled'] });
      await useSettingsStore.getState().loadModels();
    } catch (e) {
      addToast({
        type: 'error',
        message: `Could not install ${result.name}: ${e instanceof Error ? e.message : 'request failed'}`,
        duration: 8000,
      });
    } finally {
      setInstalling(false);
    }
  };

  return (
    <div className="settings-item" style={{ flexDirection: 'column', alignItems: 'stretch' }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
        <div className="settings-item-info" style={{ flex: 1 }}>
          <div
            className="settings-item-name"
            style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}
          >
            {result.name}
            <span className="model-local-badge">{result.author}</span>
            {result.param_size && (
              <span style={{ fontSize: 11, color: 'var(--text-secondary, #888)' }}>
                {result.param_size}
              </span>
            )}
          </div>
          <div className="settings-item-desc">
            {result.arch ? `${result.arch} · ` : ''}
            {humanCount(result.downloads)} downloads
            {result.gated ? ' · gated (needs license acceptance)' : ''}
          </div>
        </div>
        <div className="settings-item-actions">
          <button
            className="btn btn-sm"
            type="button"
            aria-expanded={open}
            onClick={() => setOpen((v) => !v)}
          >
            {open ? 'Hide quants' : 'Choose quant'}
          </button>
        </div>
      </div>

      {open && (
        <div style={{ marginTop: 8 }}>
          {detailQuery.isLoading && <p className="settings-empty">Loading quants…</p>}
          {detailQuery.isError && (
            <p className="settings-empty" role="alert">
              Could not load quants for this model.
            </p>
          )}
          {detail && detail.quants.length === 0 && (
            <p className="settings-empty">No compatible GGUF quants found in this repo.</p>
          )}
          {detail && detail.quants.length > 0 && (
            <>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 4 }}>
                {detail.quants.map((qopt) => (
                  <label
                    key={qopt.filename}
                    style={{
                      display: 'flex',
                      alignItems: 'center',
                      gap: 8,
                      fontSize: 13,
                      cursor: 'pointer',
                    }}
                  >
                    <input
                      type="radio"
                      name={`quant-${result.repo_id}`}
                      value={qopt.filename}
                      checked={effectiveSelected === qopt.filename}
                      onChange={() => setSelected(qopt.filename)}
                    />
                    <span style={{ fontWeight: 600, minWidth: 96 }}>{qopt.quant}</span>
                    <span style={{ color: 'var(--text-secondary, #888)' }}>
                      {humanSize(qopt.size_bytes)}
                      {qopt.is_sharded ? ` · ${qopt.shard_count} shards` : ''}
                    </span>
                    {qopt.recommended && (
                      <span
                        style={{
                          fontSize: 11,
                          fontWeight: 600,
                          padding: '1px 6px',
                          borderRadius: 999,
                          color: 'var(--accent)',
                          background: 'var(--accent-dim, rgba(99,102,241,0.15))',
                        }}
                      >
                        Recommended
                      </span>
                    )}
                  </label>
                ))}
              </div>
              <div style={{ marginTop: 8 }}>
                <button
                  className="btn btn-primary btn-sm"
                  type="button"
                  disabled={installing || !effectiveSelected}
                  onClick={() => void onInstall()}
                >
                  {installing ? 'Adding…' : 'Download'}
                </button>
              </div>
            </>
          )}
        </div>
      )}
    </div>
  );
};

/**
 * Settings > Models > Discover: search Hugging Face for GGUF chat models and
 * install one with a click. Only models the bundled engine can run are shown
 * (the backend gates on architecture). Downloads reuse the manager queue, so
 * progress/cancel appear in the download list above and the model shows in the
 * composer picker once installed. Server state lives in react-query (no
 * zustand-fresh-object selectors).
 */
export const ModelBrowser: React.FC = () => {
  const [term, setTerm] = useState('');
  const [submitted, setSubmitted] = useState('');
  const [sort, setSort] = useState<BrowseSort>('trending');
  const [allOfHf, setAllOfHf] = useState(false);

  const searchQuery = useQuery({
    queryKey: ['model-browse-search', submitted, sort, allOfHf],
    queryFn: () => searchModels({ q: submitted, sort, all: allOfHf }),
    // Only run once the user has searched — an empty trusted-author browse is
    // still useful (top trending), so we allow an empty term after submit.
    enabled: submitted !== '' || sort === 'trending',
  });

  const results = searchQuery.data?.results ?? [];

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(term.trim());
  };

  return (
    <div style={{ marginBottom: 16 }}>
      <h4 style={{ margin: '12px 0 2px' }}>Discover models</h4>
      <p className="settings-empty" style={{ margin: '0 0 8px' }}>
        Search Hugging Face for on-device GGUF chat models. Only models the local engine can run are
        shown. Pick a quant and download it, and it then appears in the list above and the chat
        model picker.
      </p>

      <form onSubmit={onSubmit} style={{ display: 'flex', gap: 8, marginBottom: 8, flexWrap: 'wrap' }}>
        <input
          type="text"
          value={term}
          onChange={(e) => setTerm(e.target.value)}
          placeholder="Search models (e.g. qwen3, gemma, mistral)…"
          aria-label="Search Hugging Face models"
          style={{ flex: 1, minWidth: 180 }}
          className="settings-input"
        />
        <select
          value={sort}
          onChange={(e) => setSort(e.target.value as BrowseSort)}
          aria-label="Sort order"
          className="settings-input"
        >
          <option value="trending">Trending</option>
          <option value="downloads">Most downloaded</option>
        </select>
        <button className="btn btn-primary btn-sm" type="submit">
          Search
        </button>
      </form>

      <label
        style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 12, marginBottom: 8 }}
      >
        <input
          type="checkbox"
          checked={allOfHf}
          onChange={(e) => setAllOfHf(e.target.checked)}
        />
        Search all of Hugging Face (default: trusted publishers only)
      </label>

      {searchQuery.isLoading && <p className="settings-empty">Searching…</p>}
      {searchQuery.isError && (
        <p className="settings-empty" role="alert">
          Search failed. Check your connection and try again.
        </p>
      )}
      {searchQuery.data && results.length === 0 && (
        <p className="settings-empty">No compatible models found. Try a different search.</p>
      )}

      <div className="settings-list">
        {results.map((r) => (
          <RepoRow key={r.repo_id} result={r} />
        ))}
      </div>
    </div>
  );
};
