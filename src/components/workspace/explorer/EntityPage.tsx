import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { getExploreEntity, type ExploreFactCite } from '@/api/workspace';
import { confidenceLabel, factSentence } from './factSentence';
import type { ExplorerTarget } from '@/stores/uiStore';

interface Props {
  workspace: string;
  name: string;
  label?: string;
  go: (target: ExplorerTarget) => void;
  openCite: (cite: ExploreFactCite) => void;
}

/** Everything about one entity, as prose: typed facts rendered as sentences
 *  with their verbatim evidence and a line-anchored citation, then the files
 *  that mention it and its most co-mentioned neighbours. */
export const EntityPage: React.FC<Props> = ({ workspace, name, label, go, openCite }) => {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['xpl-entity', workspace, name, label ?? ''],
    queryFn: () => getExploreEntity(workspace, name, label ?? ''),
  });

  if (isLoading) return <div className="xpl-status">Reading the index…</div>;
  if (isError) return <div className="xpl-status">Could not read the index.</div>;
  if (!data?.found) return <div className="xpl-status">Nothing recorded about “{name}”.</div>;

  const facts = data.facts ?? [];
  const mentioned = data.mentioned_in ?? [];
  const related = data.related ?? [];

  return (
    <div className="xpl-page">
      <div className="xpl-entity-head">
        <span className="xpl-entity-name">{data.name}</span>
        {data.label && <span className="xpl-kind-chip">{data.label}</span>}
      </div>
      {data.description && <div className="xpl-entity-desc">{data.description}</div>}

      <div className="xpl-cols">
        <div className="xpl-col-main">
          <div className="xpl-sec-head">What the documents say</div>
          {facts.length === 0 && (
            <div className="xpl-empty">
              No confirmed relationships yet. Turn on relationship mapping in this folder's index settings and reindex to collect them.
            </div>
          )}
          {facts.map((f) => (
            <div key={`${f.direction}-${f.predicate}-${f.other}`} className="xpl-fact">
              <div className="xpl-fact-sentence">
                <button type="button" className="xpl-inline-link" onClick={() => go({ kind: 'entity', name: f.other })}>
                  {factSentence(data.name ?? name, f.other, f.predicate, f.direction)}
                </button>
                <span className="xpl-conf">{confidenceLabel(f.files, f.sources)}</span>
              </div>
              {f.cite?.evidence && (
                <div className="xpl-fact-evidence">
                  “{f.cite.evidence}”{' '}
                  <button type="button" className="xpl-cite" onClick={() => openCite(f.cite!)}>
                    {f.cite.name ?? f.cite.path}
                    {f.cite.start_line ? `, line ${f.cite.start_line}` : ''}
                  </button>
                </div>
              )}
            </div>
          ))}

          {related.length > 0 && (
            <>
              <div className="xpl-sec-head">Related people and topics</div>
              <div className="xpl-chips">
                {related.map((r) => (
                  <button
                    key={r.name}
                    type="button"
                    className="xpl-chip"
                    title={`Mentioned together in ${r.shared} passage${r.shared === 1 ? '' : 's'}`}
                    onClick={() => go({ kind: 'entity', name: r.name, label: r.label })}
                  >
                    {r.name}
                  </button>
                ))}
              </div>
            </>
          )}
        </div>

        <div className="xpl-col-side">
          <div className="xpl-sec-head">
            Mentioned in {mentioned.length} file{mentioned.length === 1 ? '' : 's'}
            {data.truncated ? ' (strongest shown)' : ''}
          </div>
          {mentioned.map((m) => (
            <button key={m.path} type="button" className="xpl-row" onClick={() => go({ kind: 'file', file: m.path })}>
              <span className="xpl-row-name mono">{m.name}</span>
              <span className="xpl-row-meta">{m.passages} passage{m.passages === 1 ? '' : 's'}</span>
            </button>
          ))}
        </div>
      </div>
    </div>
  );
};
