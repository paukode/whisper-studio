import React, { useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import {
  fetchRecommendedModels,
  fetchRepoDetail,
  installModel,
  installRecommendedModel,
  searchModels,
  type BrowseResult,
  type BrowseSort,
  type InstallResult,
  type MemFit,
  type RecommendedModel,
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

/** Suffix appended to a quant's option label — plain text, since <option>
 *  cannot render markup. */
const fitSuffix = (fit: MemFit | null | undefined): string => {
  if (fit === 'too_big') return " · won't fit";
  if (fit === 'tight') return ' · tight fit';
  return '';
};

/** The warning line shown under a row when the currently-selected quant does
 *  not comfortably fit this machine. Every number comes from the API
 *  response for THIS request — nothing here is sized for any one machine. */
const FitWarning: React.FC<{
  fit: MemFit | null | undefined;
  neededBytes: number | null | undefined;
  memBudgetBytes: number | null | undefined;
}> = ({ fit, neededBytes, memBudgetBytes }) => {
  if (fit !== 'too_big' && fit !== 'tight') return null;
  const needed = humanSize(neededBytes);
  const budget = humanSize(memBudgetBytes);
  const message =
    fit === 'too_big'
      ? `Needs about ${needed} free to run, but this machine has only ~${budget} available for chat models right now. It will download but likely fail to run; pick a smaller quant.`
      : `Needs about ${needed} free to run — this machine has ~${budget} available, so it's close to the limit. May be slow or fail at a large context size.`;
  return (
    <p
      role="alert"
      style={{
        fontSize: 11,
        color:
          fit === 'too_big' ? 'var(--accent-record, #e5484d)' : 'var(--accent-warn, #b8860b)',
        margin: '4px 0 0',
      }}
    >
      {message}
    </p>
  );
};

/** The threshold to quote when the server didn't send its own. Only a display
 *  fallback — `agentic_min_params_b` from the detail response wins. */
const DEFAULT_AGENTIC_MIN_B = 7;

/** Small amber chip marking a model that installs without tool calling. Sits
 *  next to the parameter size, so the limitation is visible while scanning a
 *  result list rather than only after a multi-GB download. */
const ChatOnlyChip: React.FC = () => (
  <span
    className="model-local-badge"
    style={{ color: 'var(--accent-warn, #b8860b)', borderColor: 'var(--accent-warn, #b8860b)' }}
    title="Too small to use tools reliably — installs as a chat-only model."
  >
    chat only
  </span>
);

/** The note shown under a row for a model below the agentic size threshold.
 *  Informational rather than an error: the download is allowed and the model
 *  works fine as a chat model, it just won't be given the tool pool. */
const ToolCapabilityNote: React.FC<{ minParamsB?: number | null }> = ({ minParamsB }) => (
  <p style={{ fontSize: 11, color: 'var(--text-secondary, #888)', margin: '4px 0 0' }}>
    Under {minParamsB ?? DEFAULT_AGENTIC_MIN_B}B, so it installs with tool calling off. Models this
    small answer with a raw tool-call blob instead of running the tool, so they get no tools, no
    workspace access and a much smaller prompt. Fine for chat and quick code.
  </p>
);

type QueryClient = ReturnType<typeof useQueryClient>;

/** After any install, surface the new download row in the manager list above and
 *  refresh the composer picker so the model appears once installed. */
async function refreshAfterInstall(queryClient: QueryClient): Promise<void> {
  await queryClient.invalidateQueries({ queryKey: ['models-manager-catalog'] });
  await queryClient.invalidateQueries({ queryKey: ['models-manager-status'] });
  await queryClient.invalidateQueries({ queryKey: ['models-disabled'] });
  await useSettingsStore.getState().loadModels();
}

/** The success toast for an install, shared by search and recommended installs.
 *  Mentions index-LLM adoption when the first local install flipped it. */
function installToast(res: InstallResult) {
  const base =
    res.download === 'queued'
      ? `${res.label} queued for download. Track it under Models > Chat.`
      : res.download === 'already_installed'
        ? `${res.label} is already downloaded and now in your models.`
        : `${res.label} added and downloading. Track it under Models > Chat.`;
  const suffix = res.adopted_index_llm ? ' Indexing will now use your on-device model.' : '';
  // State the capability the install actually chose. Without this the first
  // clue that a small model is chat-only would be a turn that never uses a tool.
  const tools = res.supports_tools
    ? ''
    : ' Tool calling is off for it (too small to drive tools reliably) — it is chat only.';
  return {
    type: (res.download === 'queued' ? 'info' : 'success') as 'info' | 'success',
    message: base + suffix + tools,
    duration: res.supports_tools ? 6000 : 9000,
  };
}

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
  const selectedQuant = quants.find((q) => q.filename === effectiveSelected);
  const selectedFit = selectedQuant?.fit;
  // Known from the search row alone, so the chip and note render before the
  // quant detail is ever fetched; the detail refines it once it lands.
  const toolCapable = detail?.tool_capable ?? result.tool_capable;
  const activate = () => setActivated(true);

  const onInstall = async () => {
    if (!effectiveSelected) return;
    setInstalling(true);
    try {
      const res = await installModel({ repo_id: result.repo_id, filename: effectiveSelected });
      addToast(installToast(res));
      await refreshAfterInstall(queryClient);
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
    <div>
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
            {toolCapable === false && <ChatOnlyChip />}
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
                {fitSuffix(qopt.fit)}
              </option>
            ))}
          </select>
          <button
            className="btn btn-primary btn-sm"
            type="button"
            disabled={installing || !hasQuants || !effectiveSelected}
            onClick={() => void onInstall()}
          >
            {installing ? 'Adding…' : selectedFit === 'too_big' ? 'Download anyway' : 'Download'}
          </button>
        </div>
      </div>
      <FitWarning
        fit={selectedFit}
        neededBytes={selectedQuant?.needed_bytes}
        memBudgetBytes={detail?.mem_budget_bytes}
      />
      {toolCapable === false && (
        <ToolCapabilityNote minParamsB={detail?.agentic_min_params_b} />
      )}
    </div>
  );
};

/** One curated recommendation: label + size + quant, one-click install. Shows
 *  "Installed" once its weights are on disk. */
const RecommendedRow: React.FC<{ model: RecommendedModel }> = ({ model }) => {
  const [installing, setInstalling] = useState(false);
  const queryClient = useQueryClient();
  const addToast = useUIStore((s) => s.addToast);

  const onInstall = async () => {
    setInstalling(true);
    try {
      const res = await installRecommendedModel(model.key);
      addToast(installToast(res));
      await refreshAfterInstall(queryClient);
    } catch (e) {
      addToast({
        type: 'error',
        message: `Could not install ${model.label}: ${e instanceof Error ? e.message : 'request failed'}`,
        duration: 8000,
      });
    } finally {
      setInstalling(false);
    }
  };

  return (
    <div className="settings-item">
      <div className="settings-item-info">
        <div
          className="settings-item-name"
          style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}
        >
          {model.label}
          <span className="model-local-badge">{model.author}</span>
          {model.param_size && (
            <span style={{ fontSize: 11, color: 'var(--text-secondary, #888)' }}>
              {model.param_size}
            </span>
          )}
          {!model.supports_tools && <ChatOnlyChip />}
        </div>
        <div className="settings-item-desc">
          {model.quant}
          {model.size_bytes ? ` · ${humanSize(model.size_bytes)}` : ''}
          {model.ctx ? ` · ${Math.round(model.ctx / 1024)}K context` : ''}
          {model.fit === 'too_big' && (
            <span style={{ color: 'var(--accent-record, #e5484d)' }}>
              {' '}
              · needs {humanSize(model.needed_bytes)} free · won't fit on this machine
            </span>
          )}
          {model.fit === 'tight' && (
            <span style={{ color: 'var(--accent-warn, #b8860b)' }}>
              {' '}
              · needs {humanSize(model.needed_bytes)} free · tight fit on this machine
            </span>
          )}
        </div>
      </div>
      <div className="settings-item-actions" style={{ gap: 8 }}>
        {model.downloaded ? (
          <span className="model-installed-badge" style={{ fontSize: 12, color: 'var(--accent-live, #34c77b)' }}>
            Installed
          </span>
        ) : (
          <button
            className="btn btn-primary btn-sm"
            type="button"
            disabled={installing}
            onClick={() => void onInstall()}
          >
            {installing ? 'Adding…' : model.fit === 'too_big' ? 'Download anyway' : 'Download'}
          </button>
        )}
      </div>
    </div>
  );
};

/** The curated Recommended catalog — the primary way to get an on-device model,
 *  since the app ships none by default. */
const RecommendedSection: React.FC = () => {
  const query = useQuery({
    queryKey: ['model-browse-recommended'],
    queryFn: fetchRecommendedModels,
  });
  const models = query.data?.recommended ?? [];
  if (query.isLoading || models.length === 0) return null;
  return (
    <div style={{ marginBottom: 20 }}>
      <h5 className="models-section-title" style={{ fontSize: 13 }}>
        Recommended
      </h5>
      <p className="models-section-desc">
        Curated on-device models, ready to download. No local model ships by default.
      </p>
      <div className="settings-list">
        {models.map((m) => (
          <RecommendedRow key={m.key} model={m} />
        ))}
      </div>
    </div>
  );
};

/**
 * Settings > Models > Discover: search Hugging Face for GGUF chat models and
 * install one with a click. Only models the bundled engine can run are shown
 * (the backend gates on architecture). Downloads reuse the manager queue, so
 * progress/cancel appear under Models > Chat and the model shows in the composer
 * picker once installed. Server state lives in react-query (no
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
    // Always run — an empty-term browse is a useful default top list for BOTH
    // sorts (trending and most-downloaded). The sort/scope live in the query
    // key, so switching either refetches without touching the search term.
  });

  const results = searchQuery.data?.results ?? [];

  const onSubmit = (e: React.FormEvent) => {
    e.preventDefault();
    setSubmitted(term.trim());
  };

  return (
    <div className="models-section">
      <h4 className="models-section-title">Discover models</h4>
      <p className="models-section-desc">
        Download an on-device model. Start with a Recommended pick, or search Hugging
        Face for any GGUF chat model the local engine can run.
      </p>

      <RecommendedSection />

      <h5 className="models-section-title" style={{ fontSize: 13 }}>
        Search Hugging Face
      </h5>
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

      <div className="model-browse-scope">
        <label className="toggle-switch" title="Off: trusted publishers only. On: all of Hugging Face.">
          <input
            type="checkbox"
            checked={allOfHf}
            onChange={(e) => setAllOfHf(e.target.checked)}
            aria-label="Search all of Hugging Face"
          />
          <span className="toggle-slider"></span>
        </label>
        <span>All of Hugging Face</span>
      </div>

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
