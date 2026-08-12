/**
 * Render contract for VizCard.
 *
 * The security-relevant half: a diagram is model-authored markup rendered
 * into the app document, so DOMPurify has to strip scripts and event handlers
 * before it lands, and a chart's Vega runtime has to stay in a sandboxed
 * iframe with no `allow-same-origin`.
 *
 * The rest pins the theme contract: the model never names a colour, so the
 * class hooks that carry the theme must survive sanitisation.
 */
import { render, waitFor } from '@testing-library/react';
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { VizCard } from './VizCard';
import type { VizArtifact } from '@/types/chat';

function viz(over: Partial<VizArtifact> = {}): VizArtifact {
  return {
    kind: 'svg',
    title: 'Indexing flow',
    description: 'how a file becomes searchable',
    source: '<svg viewBox="0 0 680 100"><rect class="c-1"/><text class="th">hi</text></svg>',
    ...over,
  };
}

describe('VizCard: SVG diagrams', () => {
  it('renders the diagram inline with its theme classes intact', () => {
    const { container } = render(<VizCard viz={viz()} />);
    const svg = container.querySelector('.viz-svg svg');
    expect(svg).toBeTruthy();
    expect(container.querySelector('.viz-svg .c-1')).toBeTruthy();
    expect(container.querySelector('.viz-svg .th')?.textContent).toBe('hi');
  });

  it('strips scripts and event handlers from model-authored markup', () => {
    const { container } = render(
      <VizCard
        viz={viz({
          source:
            '<svg viewBox="0 0 680 100">' +
            '<script>fetch("/api/sessions")</script>' +
            '<rect class="box" onload="alert(1)"/>' +
            '<a href="javascript:alert(2)"><text>x</text></a>' +
            '</svg>',
        })}
      />,
    );
    const host = container.querySelector('.viz-svg')!;
    expect(host.querySelector('script')).toBeNull();
    expect(host.innerHTML).not.toContain('onload');
    expect(host.innerHTML).not.toContain('javascript:');
    // The legitimate structure survives.
    expect(host.querySelector('rect.box')).toBeTruthy();
  });

  it('shows the title and description', () => {
    const { getByText } = render(<VizCard viz={viz()} />);
    expect(getByText('Indexing flow')).toBeTruthy();
    expect(getByText('how a file becomes searchable')).toBeTruthy();
  });
});

describe('VizCard: charts', () => {
  const spec = JSON.stringify({ mark: 'bar', data: { values: [{ a: 1 }] } });
  const HOST = '<!doctype html><html><body>chart host</body></html>';

  beforeEach(() => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(HOST, { status: 200 })),
    );
  });

  it('runs the Vega runtime in an iframe that cannot reach the app', async () => {
    const { container } = render(<VizCard viz={viz({ kind: 'chart', source: spec })} />);
    await waitFor(() => expect(container.querySelector('iframe')).toBeTruthy());
    const frame = container.querySelector('iframe')!;
    expect(frame.getAttribute('srcdoc')).toContain('chart host');
    const sandbox = frame.getAttribute('sandbox') ?? '';
    expect(sandbox).toContain('allow-scripts');
    // Without this the iframe would share the app's origin and could call the
    // local backend API with the user's session.
    expect(sandbox).not.toContain('allow-same-origin');
  });

  it('reports a malformed spec instead of rendering an empty frame', () => {
    const { container, getByText } = render(
      <VizCard viz={viz({ kind: 'chart', source: '{not json' })} />,
    );
    expect(container.querySelector('iframe')).toBeNull();
    expect(getByText(/not valid JSON/)).toBeTruthy();
  });

  it('does not render an SVG host for chart kind', async () => {
    const { container } = render(<VizCard viz={viz({ kind: 'chart', source: spec })} />);
    await waitFor(() => expect(container.querySelector('iframe')).toBeTruthy());
    expect(container.querySelector('.viz-svg')).toBeNull();
  });
});
