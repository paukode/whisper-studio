import { useEffect, useMemo, useRef } from 'react';
import ForceGraph2D, { type ForceGraphMethods } from 'react-force-graph-2d';
import type { IndexGraph } from '@/api/workspace';

/**
 * Force-directed renderer for the workspace file-relationship graph, built on
 * react-force-graph-2d (canvas). This is the first of a planned set of
 * interchangeable renderers — a Cytoscape.js and a sigma.js renderer can drop
 * in behind the same {graph, width, height, selectedId, onSelect, onReveal}
 * contract.
 *
 * Interaction: single-click selects a node (it + its direct neighbours are
 * highlighted, the rest dimmed); double-click reveals the file in Finder;
 * clicking empty space clears the selection.
 */
// Palette for the unified (all-workspaces) graph — one colour per source
// workspace. The overlay's legend imports the same array so swatches match.
export const GRAPH_GROUP_COLORS = [
  '#e0a458', '#5b9bd5', '#8fce8f', '#c58fd6',
  '#d68f8f', '#8fd6cf', '#d6c68f', '#a9a9d6',
];

interface GraphNode {
  id: string;
  name: string;
  chunks?: number;
  group?: number;
  type?: 'file' | 'entity';
  community?: number;  // Leiden cluster index (community view colors by this)
  degree?: number;     // edge degree (community view sizes by this)
  ux?: number;         // UMAP x in [0,1] (umap layout pins to this)
  uy?: number;         // UMAP y in [0,1]
  x?: number;
  y?: number;
  fx?: number;         // fixed position (set in umap layout to pin the scatter)
  fy?: number;
}
interface GraphLink {
  source: string | GraphNode;
  target: string | GraphNode;
  weight: number;
  relation?: string;  // typed entity↔entity edge label (works_at, cites, …)
}

interface Props {
  graph: IndexGraph;
  width: number;
  height: number;
  selectedId: string | null;
  onSelect: (nodeId: string | null) => void;
  onReveal: (nodeId: string) => void;
  /** 'default' colors by workspace group; 'community' colors by Leiden cluster
   *  and sizes nodes by edge degree (the community view). */
  colorMode?: 'default' | 'community';
  /** 'force' runs the physics layout; 'umap' pins nodes at their 2D embedding
   *  projection (ux/uy) — the semantic-map view. */
  layout?: 'force' | 'umap';
}

const NODE_REL_SIZE = 5;
const DOUBLE_CLICK_MS = 300;

function cssVar(name: string, fallback: string): string {
  if (typeof document === 'undefined') return fallback;
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim() || fallback;
}

function endpointId(end: string | GraphNode): string {
  return typeof end === 'object' ? end.id : end;
}

// Distinct hue per community via the golden angle so adjacent indices don't look
// alike; isolated nodes (community -1 / undefined) read neutral gray.
function communityColor(idx: number | undefined): string {
  if (idx == null || idx < 0) return '#8a8a8a';
  return `hsl(${Math.round((idx * 137.508) % 360)}, 62%, 58%)`;
}

export const ForceGraphRenderer: React.FC<Props> = ({ graph, width, height, selectedId, onSelect, onReveal, colorMode = 'default', layout = 'force' }) => {
  const data = useMemo(() => {
    const S = 600;  // virtual extent for the umap scatter; zoomToFit scales it to the canvas
    return {
      nodes: graph.nodes.map((n): GraphNode => {
        const node: GraphNode = { id: n.id, name: n.name, chunks: n.chunks ?? 0, group: n.group, type: n.type, community: n.community, degree: n.degree };
        // UMAP layout: pin each node at its projected coordinate (fx/fy fix it
        // so the physics sim leaves it in place) instead of force-positioning.
        if (layout === 'umap' && typeof n.ux === 'number' && typeof n.uy === 'number') {
          node.fx = (n.ux - 0.5) * S;
          node.fy = (n.uy - 0.5) * S;
          node.x = node.fx;
          node.y = node.fy;
        }
        return node;
      }),
      links: graph.edges.map((e): GraphLink => ({ source: e.source, target: e.target, weight: e.weight ?? 1, relation: e.relation })),
    };
  }, [graph, layout]);

  // Direct neighbours of the selected node, derived from the (string-keyed)
  // edges so it's independent of react-force-graph mutating link endpoints
  // into node objects.
  const neighbors = useMemo(() => {
    const s = new Set<string>();
    if (selectedId) {
      for (const e of graph.edges) {
        if (e.source === selectedId) s.add(e.target);
        else if (e.target === selectedId) s.add(e.source);
      }
    }
    return s;
  }, [graph.edges, selectedId]);

  const colors = useMemo(() => ({
    node: cssVar('--accent', '#e0a458'),
    entity: '#39c5a8',  // distinct hue so entity (person/topic) bubbles stand out from files
    link: cssVar('--border-strong', '#555'),
    text: cssVar('--text-primary', '#e8e8e8'),
    dim: 'rgba(140,140,140,0.18)',
    linkDim: 'rgba(140,140,140,0.08)',
  }), []);

  const fgRef = useRef<ForceGraphMethods<GraphNode, GraphLink> | undefined>(undefined);
  // Spread the nodes apart — the default charge packs a small, densely-linked
  // graph into a clump. Stronger repulsion makes the structure legible.
  useEffect(() => {
    if (layout === 'umap') {
      // Pinned scatter — don't run a force layout; just fit the view to the
      // projected points once they're placed.
      const id = setTimeout(() => fgRef.current?.zoomToFit?.(400, 40), 250);
      return () => clearTimeout(id);
    }
    fgRef.current?.d3Force('charge')?.strength(-260);
    fgRef.current?.d3ReheatSimulation();
  }, [data, layout]);

  // Selection only changes colours, not positions — force a canvas redraw so
  // the highlight appears even after the simulation has cooled.
  useEffect(() => {
    (fgRef.current as { refresh?: () => void } | undefined)?.refresh?.();
  }, [selectedId, neighbors]);

  // react-force-graph has no onNodeDoubleClick, so detect it from click timing:
  // a second click on the same node within DOUBLE_CLICK_MS reveals it, otherwise
  // a click just selects.
  const lastClick = useRef<{ id: string | null; t: number }>({ id: null, t: 0 });
  const handleNodeClick = (n: GraphNode) => {
    const now = Date.now();
    if (lastClick.current.id === n.id && now - lastClick.current.t < DOUBLE_CLICK_MS) {
      lastClick.current = { id: null, t: 0 };
      onReveal(n.id);
    } else {
      lastClick.current = { id: n.id, t: now };
      onSelect(n.id);
    }
  };

  // Node radius basis: entities fixed; the community view emphasizes hubs by edge
  // degree (centrality), the default view by chunk count.
  const nodeSize = (n: GraphNode): number =>
    n.type === 'entity' ? 10
      : colorMode === 'community' ? 1 + Math.min(10, n.degree ?? 0)
      : 1 + Math.min(8, n.chunks ?? 0);

  const linkActive = (l: GraphLink): boolean =>
    !!selectedId && (endpointId(l.source) === selectedId || endpointId(l.target) === selectedId);

  return (
    <ForceGraph2D<GraphNode, GraphLink>
      ref={fgRef}
      graphData={data}
      width={width}
      height={height}
      backgroundColor="rgba(0,0,0,0)"
      nodeRelSize={NODE_REL_SIZE}
      nodeVal={nodeSize}
      nodeColor={(n) => {
        const base = n.type === 'entity'
          ? colors.entity
          : colorMode === 'community'
            ? communityColor(n.community)
            : typeof n.group === 'number'
              ? GRAPH_GROUP_COLORS[n.group % GRAPH_GROUP_COLORS.length]
              : colors.node;
        if (!selectedId) return base;
        if (n.id === selectedId || neighbors.has(n.id)) return base;
        return colors.dim;
      }}
      nodeLabel={(n) => n.name}
      linkColor={(l) => (!selectedId || linkActive(l) ? colors.link : colors.linkDim)}
      linkWidth={(l) => {
        const w = Math.min(5, 0.5 + Math.log2(1 + l.weight));
        return selectedId && linkActive(l) ? w + 1.5 : w;
      }}
      linkDirectionalArrowLength={(l) => (l.relation ? 4 : 0)}  // arrowed only for typed (directional) edges
      linkDirectionalArrowRelPos={1}
      linkCanvasObjectMode={(l) => (l.relation ? 'after' : undefined)}
      linkCanvasObject={(l, ctx, scale) => {
        // Label typed relations (works_at, cites, …) at the edge midpoint when
        // zoomed in or when the edge touches the selected node.
        if (!l.relation || (scale < 1.3 && !linkActive(l))) return;
        if (selectedId && !linkActive(l)) return;
        const s = l.source as GraphNode, t = l.target as GraphNode;
        const mx = ((s.x ?? 0) + (t.x ?? 0)) / 2, my = ((s.y ?? 0) + (t.y ?? 0)) / 2;
        const fontSize = 9 / scale;
        ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = colors.text;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'middle';
        ctx.fillText(l.relation.replace(/_/g, ' '), mx, my);
      }}
      onNodeClick={handleNodeClick}
      onBackgroundClick={() => onSelect(null)}
      cooldownTicks={120}
      nodeCanvasObjectMode={() => 'after'}
      nodeCanvasObject={(node, ctx, scale) => {
        const dimmed = !!selectedId && node.id !== selectedId && !neighbors.has(node.id);
        // Ring the selected node so it reads as the focus, distinct from its
        // (same-coloured) neighbours.
        if (node.id === selectedId) {
          const r = NODE_REL_SIZE * Math.sqrt(nodeSize(node));
          ctx.beginPath();
          ctx.arc(node.x ?? 0, node.y ?? 0, r + 3 / scale, 0, 2 * Math.PI);
          ctx.strokeStyle = colors.text;
          ctx.lineWidth = 2 / scale;
          ctx.stroke();
        }
        // Label once zoomed in, or always for the selected node + its neighbours
        // so the highlighted sub-graph is readable even when zoomed out.
        const inFocus = node.id === selectedId || neighbors.has(node.id) || node.type === 'entity';
        if (scale < 1.3 && !inFocus) return;
        if (dimmed) return;
        const label = node.name.length > 30 ? node.name.slice(0, 29) + '…' : node.name;
        const fontSize = 11 / scale;
        ctx.font = `${fontSize}px Inter, system-ui, sans-serif`;
        ctx.fillStyle = colors.text;
        ctx.textAlign = 'center';
        ctx.textBaseline = 'top';
        ctx.fillText(label, node.x ?? 0, (node.y ?? 0) + 7 / scale);
      }}
    />
  );
};
