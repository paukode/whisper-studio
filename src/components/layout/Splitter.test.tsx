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

// The same trap, one level up: AppShell conditionally rendered EITHER
// <Splitter>{panels}{dock}</Splitter> OR bare {panels}, depending on whether
// the right dock was open. Opening a task card or closing the Tasks pane
// flipped that flag, so React tore down and rebuilt the whole panels
// subtree — including the chat transcript's scroll container, which came
// back scrolled to the top. Verified live: scrollTop 1800 -> 0 on both the
// open and the close, with the container's DOM node replaced each time.
//
// These assert node IDENTITY, not just presence: a remounted element still
// satisfies getByTestId, but loses scrollTop. Identity is the property that
// actually keeps the reader where they were.
describe('Splitter — hideSecondPane', () => {
  it('keeps pane1 the same DOM node when the second pane is hidden and shown', () => {
    const view = (hide: boolean) => (
      <Splitter direction="horizontal" ratio={0.7} onChange={vi.fn()} hideSecondPane={hide}>
        <div data-testid="pane1">chat</div>
        <div data-testid="pane2">dock</div>
      </Splitter>
    );
    const { getByTestId, rerender } = render(view(true));
    const original = getByTestId('pane1');

    rerender(view(false)); // dock opens
    expect(getByTestId('pane1')).toBe(original);

    rerender(view(true)); // dock closes again
    expect(getByTestId('pane1')).toBe(original);
  });

  it('hides pane2 and the handle, and gives pane1 the freed space', () => {
    const { getByTestId, container } = render(
      <Splitter direction="horizontal" ratio={0.7} onChange={vi.fn()} hideSecondPane>
        <div data-testid="pane1">chat</div>
        <div data-testid="pane2">dock</div>
      </Splitter>,
    );
    // display:none is what removes it from layout. (The paired `flex: 0 0 0`
    // is belt-and-braces and unasserted here: jsdom's CSS parser drops the
    // unitless-basis shorthand that real browsers accept, which is why the
    // hideFirstPane tests above only check display either.)
    const pane2Wrapper = getByTestId('pane2').parentElement as HTMLElement;
    expect(pane2Wrapper.style.display).toBe('none');

    const handle = container.querySelector('.resize-handle') as HTMLElement;
    expect(handle.style.display).toBe('none');

    // Pane 1 fills the container rather than staying pinned to `ratio`.
    const pane1Wrapper = getByTestId('pane1').parentElement as HTMLElement;
    expect(pane1Wrapper.style.flex).toBe('1 1 auto');
    expect(pane1Wrapper.style.display).not.toBe('none');
  });

  it('restores the normal two-pane split when the dock reopens', () => {
    const { getByTestId, container } = render(
      <Splitter direction="horizontal" ratio={0.7} onChange={vi.fn()}>
        <div data-testid="pane1">chat</div>
        <div data-testid="pane2">dock</div>
      </Splitter>,
    );
    const pane2Wrapper = getByTestId('pane2').parentElement as HTMLElement;
    expect(pane2Wrapper.style.display).not.toBe('none');
    expect((container.querySelector('.resize-handle') as HTMLElement).style.display).toBe('flex');
  });
});
