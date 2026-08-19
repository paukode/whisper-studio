import React from 'react';
import { useQuery } from '@tanstack/react-query';
import { getExploreFile } from '@/api/workspace';
import { factSentence } from './factSentence';
import { MiniEgoGraph } from './MiniEgoGraph';
import type { ExplorerTarget } from '@/stores/uiStore';

interface Props {
  workspace: string;
  file: string;
  go: (target: ExplorerTarget) => void;
  openFile: (relPath: string, startLine?: number, endLine?: number) => void;
  revealFile: (relPath: string) => void;
}

/** One file's page: what it mentions, which files mention the same things and
 *  why (named shared entities), the facts stated in it, and the explorer's one
 *  diagram — a fully labeled ego view of its strongest connections. */
export const FilePage: React.FC<Props> = ({ workspace, file, go, openFile, revealFile }) => {
  const { data, isLoading, isError } = useQuery({
    queryKey: ['xpl-file', workspace, file],
    queryFn: () => getExploreFile(workspace, file),
  });

  if (isLoading) return <div className="xpl-status">Reading the index…</div>;
  if (isError) return <div className="xpl-status">Could not read the index.</div>;
  if (!data?.found) return <div className="xpl-status">This file is not in the index yet.</div>;

  const entities = data.entities ?? [];
  const neighbors = data.neighbors ?? [];
  const facts = data.facts ?? [];

  return (
    <div className="xpl-page">
      <div className="xpl-file-head">
        <span className="xpl-file-name mono" title={data.path}>{data.name}</span>
        <span className="xpl-file-meta">
          {data.passages} passage{data.passages === 1 ? '' : 's'} indexed · {neighbors.length}{data.neighbors_truncated ? '+' : ''} connected file{neighbors.length === 1 ? '' : 's'}
        </span>
        <span className="xpl-spacer" />
        <button type="button" className="btn btn-sm" onClick={() => openFile(file)}>Open</button>
        <button type="button" className="btn btn-sm" onClick={() => revealFile(file)}>Reveal in Finder</button>
      </div>

      <div className="xpl-cols">
        <div className="xpl-col-main">
          {entities.length > 0 && (
            <>
              <div className="xpl-sec-head">Names and topics in this file</div>
              <div className="xpl-chips">
                {entities.map((e) => (
                  <button
                    key={e.name}
                    type="button"
                    className="xpl-chip"
                    title={`${e.label ? e.label + ' · ' : ''}${e.mentions} mention${e.mentions === 1 ? '' : 's'} here — see everything about it`}
                    onClick={() => go({ kind: 'entity', name: e.name, label: e.label })}
                  >
                    {e.name}
                  </button>
                ))}
              </div>
            </>
          )}

          <div className="xpl-sec-head">Files that mention the same things</div>
          {neighbors.length === 0 && (
            <div className="xpl-empty">No other indexed file shares names or topics with this one.</div>
          )}
          {neighbors.map((n) => (
            <div key={n.path} className="xpl-neighbor">
              <button type="button" className="xpl-filelink" onClick={() => go({ kind: 'file', file: n.path })}>
                {n.name}
              </button>
              <span className="xpl-neighbor-why">
                shares{' '}
                {n.entities.slice(0, 3).map((en, i) => (
                  <React.Fragment key={en}>
                    {i > 0 && ', '}
                    <button type="button" className="xpl-inline-link" onClick={() => go({ kind: 'entity', name: en })}>{en}</button>
                  </React.Fragment>
                ))}
                {n.shared > Math.min(n.entities.length, 3) && ` + ${n.shared - Math.min(n.entities.length, 3)} more`}
              </span>
            </div>
          ))}

          {facts.length > 0 && (
            <>
              <div className="xpl-sec-head">Facts stated in this file</div>
              {facts.map((f) => (
                <div key={`${f.source}-${f.predicate}-${f.target}`} className="xpl-fact">
                  <div className="xpl-fact-sentence">{factSentence(f.source, f.target, f.predicate, 'out')}</div>
                  {f.evidence && (
                    <div className="xpl-fact-evidence">
                      “{f.evidence}”{' '}
                      {f.start_line != null && (
                        <button type="button" className="xpl-cite" onClick={() => openFile(file, f.start_line ?? undefined, f.end_line ?? undefined)}>
                          line {f.start_line}
                        </button>
                      )}
                    </div>
                  )}
                </div>
              ))}
            </>
          )}
        </div>

        {neighbors.length > 0 && (
          <div className="xpl-col-side">
            <MiniEgoGraph
              centerName={data.name ?? file}
              neighbors={neighbors.map((n) => ({ path: n.path, name: n.name, topEntity: n.entities[0] }))}
              totalConnections={neighbors.length}
              onSelect={(p) => go({ kind: 'file', file: p })}
            />
          </div>
        )}
      </div>
    </div>
  );
};
