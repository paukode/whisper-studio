import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { useUIStore } from '@/stores/uiStore';
import { getIndexGraph, getAllIndexesGraph, getIndexEntityGraph, getIndexUmapGraph, getAllIndexesUmapGraph } from '@/api/workspace';
import { post } from '@/api/client';
import { ForceGraphRenderer, GRAPH_GROUP_COLORS } from './graph/ForceGraphRenderer';

type GraphScope = 'one' | 'all' | 'entity';

type ViewKind = 'force' | 'communities' | 'umap';

/**
 * Full-screen overlay showing the file-relationship graph for an indexed
 * workspace. Opened from the connect dialog's indexed-workspace list. The view
 * is swappable: "Force" (default coloring) and "Communities" (Leiden clusters,
 * nodes sized by connection degree) are live; "UMAP map" (a semantic embedding
 * projection of the files) is the remaining planned view.
 */
export const WorkspaceGraphOverlay: React.FC = () => {
  const workspace = useUIStore((s) => s.graphWorkspace);
  const close = useUIStore((s) => s.closeIndexGraph);

  const [view, setView] = useState<ViewKind>('force');
  const [scope, setScope] = useState<GraphScope>('one');
  const [entity, setEntity] = useState<{ name: string; label?: string } | null>(null);
  const [size, setSize] = useState({ w: 800, h: 600 });
  const [selectedNode, setSelectedNode] = useState<string | null>(null);
  const bodyRef = useRef<HTMLDivElement>(null);

  // Scope wins over view: "All indexed" spans every workspace in Force AND UMAP
  // (a cross-workspace UMAP), instead of UMAP silently falling back to one folder.
  const { data: graph, isLoading, isError } = useQuery({
    queryKey:
      scope === 'all' ? (view === 'umap' ? ['index-graph-umap-all'] : ['index-graph-all'])
      : view === 'umap' ? ['index-graph-umap', workspace]
      : scope === 'entity' ? ['index-graph-entity', workspace, entity?.name, entity?.label]
      : ['index-graph', workspace],
    queryFn: () =>
      scope === 'all'
        ? (view === 'umap' ? getAllIndexesUmapGraph() : getAllIndexesGraph())
      : view === 'umap' ? getIndexUmapGraph(workspace as string)
      : scope === 'entity' && entity ? getIndexEntityGraph(workspace as string, entity.name, entity.label ?? '')
      : getIndexGraph(workspace as string),
    enabled: !!workspace && (scope !== 'entity' || !!entity),
  });

  // Pivot to the entity-centric view: one entity at the centre, every file that
  // mentions it around it. Pre-selecting the entity node surfaces its file list
  // in the detail panel as soon as the graph loads.
  const pivotToEntity = useCallback((name: string, label?: string) => {
    setScope('entity');
    setEntity({ name, label });
    setSelectedNode(`entity::${name}`);
  }, []);

  // Size the canvas to the body via ResizeObserver (a subscription, so no
  // synchronous setState-in-effect); fires immediately on observe. Keyed on
  // `workspace` so it re-runs once the body actually renders (the overlay is
  // mounted at app root and only renders its body when a workspace is open).
  useEffect(() => {
    const el = bodyRef.current;
    if (!el) return;
    const ro = new ResizeObserver(() => setSize({ w: el.clientWidth, h: el.clientHeight }));
    ro.observe(el);
    return () => ro.disconnect();
  }, [workspace]);

  useEffect(() => {
    if (!workspace) return;
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [workspace, close]);

  const revealNode = useCallback((nodeId: string) => {
    // Unified-graph node ids are already absolute paths; per-workspace ids are
    // relative to the graph root.
    const root = graph?.root;
    const abs = nodeId.startsWith('/')
      ? nodeId
      : root ? root.replace(/\/+$/, '') + '/' + nodeId : null;
    if (!abs) return;
    void post('/api/workspace/reveal', { path: abs }).catch(() => {
      useUIStore.getState().addToast({ type: 'error', message: 'Could not reveal the file.' });
    });
  }, [graph]);

  // Derive the live selection (rather than resetting state in an effect): a
  // stale id from a previous workspace simply doesn't match the current graph.
  const selectedId = useMemo(
    () => (selectedNode && graph?.nodes.some((n) => n.id === selectedNode) ? selectedNode : null),
    [selectedNode, graph],
  );

  // The selected file's connections: every shared-entity edge touching it, with
  // the other file's name and the entities that link them, strongest first.
  const detail = useMemo(() => {
    if (!selectedId || !graph) return null;
    const self = graph.nodes.find((n) => n.id === selectedId);
    if (!self) return null;
    const nameOf = (id: string) => graph.nodes.find((n) => n.id === id)?.name ?? id;
    const links = graph.edges
      .map((e) => {
        const other = e.source === selectedId ? e.target : e.target === selectedId ? e.source : null;
        return other ? { id: other, name: nameOf(other), weight: e.weight, entities: e.entities ?? [] } : null;
      })
      .filter((l): l is { id: string; name: string; weight: number; entities: string[] } => l !== null)
      .sort((a, b) => b.weight - a.weight);
    return { self, links };
  }, [selectedId, graph]);

  if (!workspace) return null;

  const name = workspace.split('/').filter(Boolean).pop() || workspace;
  const views: ViewKind[] = ['force', 'communities', 'umap'];
  const labels: Record<ViewKind, string> = { force: 'Force', communities: 'Communities', umap: 'UMAP map' };
  const liveViews = new Set<ViewKind>(['force', 'communities', 'umap']);

  return (
    <div className="graph-overlay" onClick={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="graph-dialog">
        <div className="graph-header">
          <div className="graph-title">
            Relationship graph<span className="graph-sub">{scope === 'all' ? 'all indexed workspaces' : scope === 'entity' ? `about “${entity?.name}”` : name}</span>
          </div>
          <div className="graph-scope-select">
            <button
              type="button"
              className={`graph-renderer-btn${scope === 'one' ? ' active' : ''}`}
              title="Just this folder's files"
              onClick={() => { setScope('one'); setSelectedNode(null); setEntity(null); }}
            >
              This folder
            </button>
            <button
              type="button"
              className={`graph-renderer-btn${scope === 'all' ? ' active' : ''}`}
              title="Every indexed workspace, linked across folders"
              onClick={() => { setScope('all'); setSelectedNode(null); setEntity(null); }}
            >
              All indexed
            </button>
            {scope === 'entity' && (
              <button
                type="button"
                className="graph-renderer-btn active"
                title="Back to the file graph"
                onClick={() => { setScope('one'); setSelectedNode(null); setEntity(null); }}
              >
                &#8592; {entity?.name}
              </button>
            )}
          </div>
          <div className="graph-renderer-select">
            {views.map((v) => (
              <button
                key={v}
                type="button"
                className={`graph-renderer-btn${view === v ? ' active' : ''}`}
                disabled={!liveViews.has(v)}
                title={
                  v === 'force' ? 'Force-directed (default coloring)'
                  : v === 'communities' ? 'Color by detected community (Leiden); size by connections'
                  : 'Semantic map: files close in meaning sit together (spans all folders in “All indexed”)'
                }
                onClick={() => setView(v)}
              >
                {labels[v]}
              </button>
            ))}
          </div>
          <button type="button" className="graph-close" onClick={close} aria-label="Close">&#xD7;</button>
        </div>
        <div className="graph-body" ref={bodyRef}>
          {isLoading && <div className="graph-status">Loading graph…</div>}
          {isError && <div className="graph-status">Could not load the relationship graph.</div>}
          {!isLoading && !isError && graph && graph.nodes.length === 0 && (
            <div className="graph-status">Nothing indexed to graph yet.</div>
          )}
          {!isLoading && !isError && graph && graph.nodes.length > 0 && (view === 'force' || view === 'communities' || view === 'umap') && (
            <ForceGraphRenderer
              graph={graph}
              width={size.w}
              height={size.h}
              selectedId={selectedId}
              onSelect={setSelectedNode}
              onReveal={revealNode}
              colorMode={
                scope === 'all'
                  ? 'default' // color by source workspace across folders
                  : view === 'communities' || view === 'umap'
                    ? 'community'
                    : 'default'
              }
              layout={view === 'umap' ? 'umap' : 'force'}
            />
          )}
          {scope === 'all' && graph && graph.workspaces && graph.workspaces.length > 0 && (
            <div className="graph-legend">
              {graph.workspaces.map((w) => (
                <span key={w.path} className="graph-legend-item" title={w.path}>
                  <span
                    className="graph-legend-dot"
                    style={{ background: GRAPH_GROUP_COLORS[w.group % GRAPH_GROUP_COLORS.length] }}
                  />
                  {w.name} <span className="graph-legend-count">{w.files}</span>
                </span>
              ))}
            </div>
          )}
          {detail && (
            <div className="graph-detail">
              <div className="graph-detail-head">
                <span className="graph-detail-name" title={selectedId ?? ''}>{detail.self.name}</span>
                <button
                  type="button"
                  className="graph-detail-reveal"
                  onClick={() => selectedId && revealNode(selectedId)}
                >
                  Reveal in Finder
                </button>
              </div>
              {detail.self.description && (
                <div
                  className="graph-detail-desc"
                  style={{ fontSize: '12.5px', fontStyle: 'italic', color: 'var(--text-secondary, #9aa3b2)', margin: '2px 0 6px', lineHeight: 1.45 }}
                >
                  {detail.self.description}
                </div>
              )}
              <div className="graph-detail-meta">
                {detail.self.chunks != null && `${detail.self.chunks} chunks · `}
                {detail.links.length} {scope === 'entity' ? 'file' : 'linked file'}{detail.links.length === 1 ? '' : 's'}
              </div>
              <div className="graph-detail-links">
                {detail.links.length === 0 && (
                  <div className="graph-detail-empty">No files share entities with this one.</div>
                )}
                {detail.links.map((l) => (
                  <button
                    key={l.id}
                    type="button"
                    className="graph-detail-link"
                    title="Select this file"
                    onClick={() => setSelectedNode(l.id)}
                  >
                    <div className="graph-detail-link-top">
                      <span className="graph-detail-link-name">{l.name}</span>
                      <span className="graph-detail-link-weight" title="shared entities">{l.weight}</span>
                    </div>
                    {l.entities.length > 0 && (
                      <div className="graph-detail-chips">
                        {l.entities.map((e) => (
                          <span
                            key={e}
                            className="graph-detail-chip graph-detail-chip-clickable"
                            role="button"
                            tabIndex={0}
                            title={`See every file about “${e}”`}
                            onClick={(ev) => { ev.stopPropagation(); pivotToEntity(e); }}
                            onKeyDown={(ev) => { if (ev.key === 'Enter') { ev.stopPropagation(); pivotToEntity(e); } }}
                          >{e}</span>
                        ))}
                      </div>
                    )}
                  </button>
                ))}
              </div>
            </div>
          )}
        </div>
        <div className="graph-footer">
          {graph && graph.nodes.length > 0
            ? `${graph.nodes.length} files · ${graph.edges.length} links${graph.truncated ? ' (strongest shown)' : ''} — click a node to explore, double-click to reveal in Finder`
            : ''}
        </div>
      </div>
    </div>
  );
};
