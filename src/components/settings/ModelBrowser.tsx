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

/** Compact quant picker for one repo: a single dropdown of quants (recommended
 *  preselected, size in each label) next to one Download button, on one row.
 *  The quant list is still fetched lazily — on first interaction with the
 *  select — so browsing a long result list doesn't hammer the detail endpoint.
 *  Recommended default, size display, sharded handling, and the install call
 *  are unchanged from the old expandable radio list. */
const RepoRow: React.FC<{ result: BrowseResult }> = ({ result }) => {
  // `activated` flips true on the first interaction with the select, which
  // enables the detail fetch. Until then the row shows a placeholder option and
  // a disabled Download, so N results don't trigger N detail requests up front.
  const [activated, setActivated] = useState(false);
  const [selected, setSelected] = useState<string | null>(null);
  const [installing, setInstalling] = useState(false);
  const queryClient = useQueryClient();
  const addToast = useUIStore((s) => s.addToast);

  const detailQuery = useQuery({
    queryKey: ['model-browse-repo', result.repo_id],
    queryFn: () => fetchRepoDetail(result.repo_id),
    enabled: activated,
  });

  // Pre-select the recommended quant once the detail lands (only if nothing
  // picked yet, so a user's manual pick is never overridden by a refetch).
  const detail = detailQuery.data;
  const quants = detail?.quants ?? [];
  const effectiveSelected =
    selected ?? detail?.recommended_filename ?? quants[0]?.filename ?? null;
  const activate = () => setActivated(true);

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

  // The one status the select must surface before real options exist.
  const placeholder = detailQuery.isError
    ? 'Could not load quants'
    : detailQuery.isLoading
      ? 'Loading quants…'
      : detail && quants.length === 0
        ? 'No compatible quants'
        : 'Choose a quant';

  const selectId = `quant-select-${result.repo_id}`;
  const hasQuants = quants.length > 0;

  return (
    <div className="settings-item">
      <div className="settings-item-info">
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
      <div className="settings-item-actions" style={{ gap: 8 }}>
        <label htmlFor={selectId} className="sr-only">
          Choose a quant for {result.name}
        </label>
        <select
          id={selectId}
          className="settings-input model-quant-select"
          value={effectiveSelected ?? ''}
          disabled={installing}
          onFocus={activate}
          onMouseDown={activate}
          onChange={(e) => setSelected(e.target.value)}
        >
          {!hasQuants && <option value="">{placeholder}</option>}
          {quants.map((qopt) => (
            <option key={qopt.filename} value={qopt.filename}>
              {qopt.quant} · {humanSize(qopt.size_bytes)}
              {qopt.is_sharded ? ` · ${qopt.shard_count} shards` : ''}
              {qopt.recommended ? ' (recommended)' : ''}
            </option>
          ))}
        </select>
        <button
          className="btn btn-primary btn-sm"
          type="button"
          disabled={installing || !hasQuants || !effectiveSelected}
          onClick={() => void onInstall()}
        >
          {installing ? 'Adding…' : 'Download'}
        </button>
      </div>
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
