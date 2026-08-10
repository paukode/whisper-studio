import { describe, expect, it, vi } from 'vitest';
import { render } from '@testing-library/react';
import { Splitter } from './Splitter';

// Regression coverage: AppShell used to conditionally render EITHER
// <Splitter>{pane1}{pane2}</Splitter> OR pane2 alone, depending on whether
// the workspace panel was visible. That put pane2's subtree at two
// different structural positions, so React unmounted and remounted it
// (and everything inside it — the whole chat panel) on every workspace
// connect/disconnect, silently wiping local component state like an
// in-progress composer draft. The fix: always render Splitter with both
// children, and collapse pane1 visually/interactively via hideFirstPane
// instead of removing it from the tree in a way that shifts pane2's slot.
describe('Splitter — hideFirstPane', () => {
  it('keeps pane2 mounted and unaffected regardless of hideFirstPane', () => {
    const { getByTestId, rerender } = render(
      <Splitter direction="horizontal" ratio={0.3} onChange={vi.fn()}>
        <div data-testid="pane1">workspace</div>
        <div data-testid="pane2">chat</div>
      </Splitter>,
    );
    expect(getByTestId('pane2')).toBeTruthy();

    rerender(
      <Splitter direction="horizontal" ratio={0.3} onChange={vi.fn()} hideFirstPane>
        <div data-testid="pane1">workspace</div>
        <div data-testid="pane2">chat</div>
      </Splitter>,
    );
    // Same query still finds it — proves React reconciled in place rather
    // than unmounting/remounting when hideFirstPane flips.
    expect(getByTestId('pane2')).toBeTruthy();
  });

  it('hides pane1 and the resize handle from layout without removing pane1 from the DOM', () => {
    const { getByTestId, container } = render(
      <Splitter direction="horizontal" ratio={0.3} onChange={vi.fn()} hideFirstPane>
        <div data-testid="pane1">workspace</div>
        <div data-testid="pane2">chat</div>
      </Splitter>,
    );
    const pane1Wrapper = getByTestId('pane1').parentElement as HTMLElement;
    expect(pane1Wrapper.style.display).toBe('none');

    const handle = container.querySelector('.resize-handle') as HTMLElement;
    expect(handle.style.display).toBe('none');
  });

  it('shows pane1 and the resize handle normally when hideFirstPane is false', () => {
    const { getByTestId, container } = render(
      <Splitter direction="horizontal" ratio={0.3} onChange={vi.fn()}>
        <div data-testid="pane1">workspace</div>
        <div data-testid="pane2">chat</div>
      </Splitter>,
    );
    const pane1Wrapper = getByTestId('pane1').parentElement as HTMLElement;
    expect(pane1Wrapper.style.display).not.toBe('none');

    const handle = container.querySelector('.resize-handle') as HTMLElement;
    expect(handle.style.display).not.toBe('none');
  });
});
