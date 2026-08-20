import React from 'react';

/**
 * The one diagram in the index explorer: a deterministic radial SVG of a file
 * and its strongest connections, every line captioned with the entity that
 * creates it. No force simulation, no size/color encodings — uniform dots,
 * words on everything. Clicking a neighbour re-centres the file page on it.
 */

export interface EgoNeighbor {
  path: string;
  name: string;
  /** The strongest shared entity — drawn on the connecting line. */
  topEntity?: string;
}

interface Props {
  centerName: string;
  neighbors: EgoNeighbor[];
  /** The file's TRUE connection count (may exceed the neighbors provided). */
  totalConnections: number;
  onSelect: (path: string) => void;
}

const W = 460;
const H = 330;
const CX = W / 2;
const CY = H / 2 - 8;
const R = 118;
const MAX_SPOKES = 8;

function trunc(s: string, n: number): string {
  return s.length > n ? s.slice(0, n - 1) + '…' : s;
}

export const MiniEgoGraph: React.FC<Props> = ({ centerName, neighbors, totalConnections, onSelect }) => {
  const spokes = neighbors.slice(0, MAX_SPOKES).map((nb, i, arr) => {
    // Start at 12 o'clock and walk clockwise, evenly spaced.
    const angle = -Math.PI / 2 + (2 * Math.PI * i) / arr.length;
    const x = CX + R * Math.cos(angle);
    const y = CY + R * Math.sin(angle);
    // Entity caption runs ALONG its own spoke (rotated text at the midpoint,
    // nudged to the upper side of the stroke), so each caption stays in its
    // own radial corridor and never collides with other spokes' captions or
    // the file labels. Left-half angles flip 180 degrees to stay readable.
    let deg = (angle * 180) / Math.PI;
    const flipped = deg > 90 || deg < -90;
    if (flipped) deg -= 180;
    const side = flipped ? -1 : 1;
    const lx = CX + (x - CX) * 0.52 + Math.sin(angle) * 6 * side;
    const ly = CY + (y - CY) * 0.52 - Math.cos(angle) * 6 * side;
    const labelY = y > CY + 4 ? y + 18 : y - 12;
    return { ...nb, x, y, lx, ly, deg, labelY };
  });

  return (
    <figure className="xpl-ego">
      <svg
        viewBox={`0 0 ${W} ${H}`}
        role="img"
        aria-label={`${centerName} and its ${spokes.length} strongest connection${spokes.length === 1 ? '' : 's'}, each labeled with the entity they share`}
      >
        <g className="xpl-ego-lines">
          {spokes.map((s) => (
            <line key={`l-${s.path}`} x1={CX} y1={CY} x2={s.x} y2={s.y} />
          ))}
        </g>
        <g className="xpl-ego-captions">
          {spokes.map((s) =>
            s.topEntity ? (
              <text
                key={`c-${s.path}`}
                x={s.lx}
                y={s.ly}
                textAnchor="middle"
                transform={`rotate(${s.deg.toFixed(1)}, ${s.lx.toFixed(1)}, ${s.ly.toFixed(1)})`}
              >
                {trunc(s.topEntity, 20)}
              </text>
            ) : null,
          )}
        </g>
        {spokes.map((s) => (
          <g
            key={`n-${s.path}`}
            className="xpl-ego-node"
            role="button"
            tabIndex={0}
            aria-label={`Show connections of ${s.name}`}
            onClick={() => onSelect(s.path)}
            onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); onSelect(s.path); } }}
          >
            <circle cx={s.x} cy={s.y} r={7} />
            <text x={s.x} y={s.labelY} textAnchor="middle">{trunc(s.name, 22)}</text>
          </g>
        ))}
        <g className="xpl-ego-center">
          <circle cx={CX} cy={CY} r={12} />
          <text x={CX} y={CY + 26} textAnchor="middle">{trunc(centerName, 26)}</text>
        </g>
      </svg>
      <figcaption>
        {totalConnections > spokes.length
          ? `Top ${spokes.length} of ${totalConnections} connections. Click a file to re-centre on it.`
          : 'Each line is labeled with the strongest shared name. Click a file to re-centre on it.'}
      </figcaption>
    </figure>
  );
};
