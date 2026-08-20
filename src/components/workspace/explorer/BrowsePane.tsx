import React, { useEffect, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { getExploreEntities } from '@/api/workspace';
import type { ExplorerTarget } from '@/stores/uiStore';

interface Props {
  workspace: string;
  /** Live search text from the shell's search box (filters the lists). */
  query: string;
  selected: { name: string } | null;
  go: (target: ExplorerTarget) => void;
}

const PREVIEW_ROWS = 8;

/** The browse pane: every extracted name grouped into People, Organizations,
 *  Topics, and Places, each row saying how many files mention it. Low-signal
 *  names are hidden but honestly counted, with a toggle to show them. */
export const BrowsePane: React.FC<Props> = ({ workspace, query, selected, go }) => {
  const [showLow, setShowLow] = useState(false);
  const [expanded, setExpanded] = useState<Record<string, boolean>>({});

  // Debounce the shell's live search text so the list doesn't refetch on every
  // keystroke. setDebounced runs in the timer callback (asynchronous), so this
  // is not a setState-in-effect violation.
  const [debounced, setDebounced] = useState('');
  useEffect(() => {
    const t = setTimeout(() => setDebounced(query.trim()), 250);
    return () => clearTimeout(t);
  }, [query]);

  const { data, isLoading } = useQuery({
    queryKey: ['xpl-entities', workspace, debounced, showLow],
    queryFn: () => getExploreEntities(workspace, debounced, showLow),
  });

  if (isLoading && !data) return <div className="xpl-status">Loading names…</div>;
  if (!data) return null;

  const groups = data.groups.filter((g) => g.entities.length > 0);
  return (
    <div className="xpl-browse">
      {groups.length === 0 && (
        <div className="xpl-empty">{debounced ? `Nothing matches “${debounced}”.` : 'No names extracted yet.'}</div>
      )}
      {groups.map((g) => {
        const isOpen = !!expanded[g.key] || !!debounced;
        const rows = isOpen ? g.entities : g.entities.slice(0, PREVIEW_ROWS);
        return (
          <div key={g.key} className="xpl-group">
            <div className="xpl-group-head">
              {g.title} · {g.entities.length}
            </div>
            {rows.map((e) => (
              <button
                key={e.name}
                type="button"
                className={`xpl-row${selected && selected.name.toLowerCase() === e.name.toLowerCase() ? ' active' : ''}`}
                title={e.description || e.label}
                onClick={() => go({ kind: 'entity', name: e.name, label: e.label })}
              >
                <span className="xpl-row-name">{e.name}</span>
                <span className="xpl-row-meta">in {e.files} file{e.files === 1 ? '' : 's'}</span>
              </button>
            ))}
            {!isOpen && g.entities.length > PREVIEW_ROWS && (
              <button
                type="button"
                className="xpl-show-more"
                onClick={() => setExpanded((x) => ({ ...x, [g.key]: true }))}
              >
                Show all {g.entities.length}
              </button>
            )}
          </div>
        );
      })}
      {data.hidden > 0 && (
        <button type="button" className="xpl-show-more" onClick={() => setShowLow((v) => !v)}>
          {showLow ? 'Hide low-signal names' : `Show ${data.hidden} low-signal name${data.hidden === 1 ? '' : 's'}`}
        </button>
      )}
    </div>
  );
};
