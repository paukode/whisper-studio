import React, { useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getAnswerSources, type AnswerSource } from '@/api/sessions';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { useDockStore } from '@/stores/dockStore';

interface Props {
  grounding: { searched: number; passages: number; id?: string };
}

/** The chip's summary line — same copy whether it's a static line or the
 *  expandable button's label. */
function chipLabel(g: Props['grounding']): string {
  const folders = `${g.searched} indexed folder${g.searched === 1 ? '' : 's'}`;
  return g.passages > 0
    ? `Grounded in ${folders} · ${g.passages} passage${g.passages === 1 ? '' : 's'}`
    : `Searched ${folders} · no matches`;
}

/** Plain-words provenance for one passage. Entity/related passages append
 *  entity chips after the colon (rendered by the caller). */
function kindLabel(kind: AnswerSource['kind']): string {
  switch (kind) {
    case 'keyword':
      return 'matched your words';
    case 'semantic':
      return 'similar meaning';
    default:
      return 'linked through shared entities:';
  }
}

const SourceRow: React.FC<{ source: AnswerSource }> = ({ source }) => {
  const name = source.path?.split('/').pop() || source.path || source.abs;
  const lines =
    source.start_line != null && source.end_line != null
      ? `:${source.start_line}-${source.end_line}`
      : '';
  const showChips =
    (source.kind === 'entity' || source.kind === 'related') && source.entities.length > 0;
  return (
    <div className="grounding-source">
      <div className="grounding-source-head">
        <button
          type="button"
          className="grounding-filelink mono"
          title={`${source.abs} · click to open at the cited lines`}
          onClick={() =>
            useDockStore.getState().openFile({
              path: source.abs,
              title: name,
              startLine: source.start_line ?? undefined,
              endLine: source.end_line ?? undefined,
            })
          }
        >
          {name}
          {lines}
        </button>
        <span className="grounding-kind">{kindLabel(source.kind)}</span>
        {showChips &&
          source.entities.map((entity) => (
            <button
              key={entity}
              type="button"
              className="xpl-chip"
              title={`See everything about ${entity} in the index explorer`}
              onClick={() => {
                if (source.ws) {
                  useUIStore.getState().openIndexExplorer(source.ws, { kind: 'entity', name: entity });
                }
              }}
            >
              {entity}
            </button>
          ))}
      </div>
      {source.snippet && <div className="grounding-snippet">{source.snippet}</div>}
    </div>
  );
};

/**
 * "Answer sources": the index-grounding chip under an assistant message.
 *
 * Counts-only turns (no persisted sources — pre-feature answers, zero-match
 * searches, or a trimmed row) render the original static line. When the
 * grounding event carried a persisted id, the chip becomes a button that
 * expands into the actual passages behind the answer, each labeled with HOW
 * it was retrieved — matched your words (keyword), similar meaning (dense
 * vector), or linked through shared entities (entity anchors / graph hop,
 * with entity chips that deep-link into the index explorer).
 */
export const AnswerSources: React.FC<Props> = ({ grounding }) => {
  const [open, setOpen] = useState(false);
  const sessionId = useSessionStore((s) => s.currentSessionId);
  const groundingId = grounding.passages > 0 ? grounding.id : undefined;

  const { data, isLoading, isError } = useQuery({
    queryKey: ['answer-sources', sessionId, groundingId],
    queryFn: () => getAnswerSources(sessionId!, groundingId!),
    enabled: open && !!sessionId && !!groundingId,
    staleTime: Infinity, // persisted rows are immutable
    retry: false, // a 404 (trimmed row) never heals by retrying
  });

  if (!groundingId) {
    return (
      <div className="grounding-chip">
        <span aria-hidden="true">{'🔎'}</span>
        {chipLabel(grounding)}
      </div>
    );
  }

  return (
    <div className="grounding-wrap">
      <button
        type="button"
        className="grounding-chip grounding-chip-btn"
        aria-expanded={open}
        title={open ? 'Hide the passages behind this answer' : 'Show the passages behind this answer'}
        onClick={() => setOpen((v) => !v)}
      >
        <span aria-hidden="true">{'🔎'}</span>
        {chipLabel(grounding)}
        <span aria-hidden="true" className="grounding-caret">
          {open ? '▾' : '▸'}
        </span>
      </button>
      {open && (
        <div className="grounding-panel">
          {isLoading && <div className="grounding-status">Loading sources…</div>}
          {isError && (
            <div className="grounding-status">
              The sources for this answer are no longer available.
            </div>
          )}
          {data?.sources.map((s, i) => (
            <SourceRow key={`${s.abs}-${s.start_line}-${i}`} source={s} />
          ))}
        </div>
      )}
    </div>
  );
};
