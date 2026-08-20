import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { getExploreOverview } from '@/api/workspace';
import { formatRelative } from '../workspaceConnectHelpers';
import type { ExplorerTarget } from '@/stores/uiStore';

interface Props {
  workspace: string;
  go: (target: ExplorerTarget) => void;
}

/** The explorer's landing page: what is in this folder, in words and counts.
 *  Topic cards claim co-mention ("files mentioning: …"), never aboutness. */
export const OverviewPage: React.FC<Props> = ({ workspace, go }) => {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['xpl-overview', workspace],
    queryFn: () => getExploreOverview(workspace),
  });

  if (isLoading) return <div className="xpl-status">Reading the index…</div>;
  if (isError || !data) return <div className="xpl-status">Could not read the index.</div>;
  if (!data.files) {
    return (
      <div className="xpl-status">
        Nothing indexed yet. Index this folder from the workspace list to explore it.
      </div>
    );
  }

  return (
    <div className="xpl-page">
      <div className="xpl-stats">
        {data.files} file{data.files === 1 ? '' : 's'} · {data.passages} passage{data.passages === 1 ? '' : 's'} · {data.entities === 1 ? '1 name or topic' : `${data.entities} names and topics`}
        {data.last_indexed_at ? ` · indexed ${formatRelative(data.last_indexed_at)}` : ''}
      </div>

      {data.cards.length > 0 && (
        <>
          <div className="xpl-rule">Files are grouped together when they mention the same people, organizations, and topics.</div>
          <div className="xpl-cards">
            {data.cards.map((c) => (
              <div key={c.id} className="xpl-card">
                <div className="xpl-card-title">
                  {c.names.length > 0 ? c.names.join(' · ') : 'A group of related files'}
                </div>
                <div className="xpl-card-count">{c.files} connected files</div>
                <div className="xpl-card-files">
                  {c.top_files.map((f) => (
                    <button key={f.path} type="button" className="xpl-filelink" onClick={() => go({ kind: 'file', file: f.path })}>
                      {f.name}
                    </button>
                  ))}
                  {c.files > c.top_files.length && (
                    <span className="xpl-more">+ {c.files - c.top_files.length} more</span>
                  )}
                </div>
              </div>
            ))}
          </div>
        </>
      )}

      <div className="xpl-lists">
        {data.stands_out.length > 0 && (
          <div className="xpl-list">
            <div className="xpl-list-head">Stands out most</div>
            {data.stands_out.map((e) => (
              <button key={e.name} type="button" className="xpl-row" onClick={() => go({ kind: 'entity', name: e.name, label: e.label })}>
                <span className="xpl-row-name">{e.name}</span>
                <span className="xpl-row-meta">{e.label || `${e.files} file${e.files === 1 ? '' : 's'}`}</span>
              </button>
            ))}
          </div>
        )}
        {data.most_connected.length > 0 && (
          <div className="xpl-list">
            <div className="xpl-list-head">Most connected files</div>
            {data.most_connected.map((f) => (
              <button key={f.path} type="button" className="xpl-row" onClick={() => go({ kind: 'file', file: f.path })}>
                <span className="xpl-row-name mono">{f.name}</span>
                <span className="xpl-row-meta">{f.links} link{f.links === 1 ? '' : 's'}</span>
              </button>
            ))}
          </div>
        )}
      </div>
      {data.truncated && (
        <div className="xpl-honesty">Connection counts consider the strongest links only.</div>
      )}
    </div>
  );
};
