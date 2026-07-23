import { describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';
import { ContextMenu, type MenuItem } from './ContextMenu';

function makeItems(onPin = vi.fn(), onSub = vi.fn()): MenuItem[] {
  return [
    { label: 'Pin', onClick: onPin },
    { label: 'Rename', onClick: vi.fn() },
    { separator: true, label: '' },
    {
      label: 'Open in',
      children: [
        { label: 'VS Code', onClick: vi.fn() },
        { label: 'Kiro', onClick: onSub },
      ],
    },
  ];
}

const POS = { x: 10, y: 10 };

describe('ContextMenu letter shortcuts', () => {
  it('renders first-letter keycaps in uppercase, skipping separators', () => {
    render(<ContextMenu items={makeItems()} position={POS} onClose={vi.fn()} letterShortcuts />);
    const caps = document.querySelectorAll('.ws-ctx-keycap');
    // Pin=P, Rename=R, Open in=O — the separator takes no letter.
    expect(Array.from(caps).map((c) => c.textContent)).toEqual(['P', 'R', 'O']);
  });

  it('label letter fires the matching top-level item and closes', () => {
    const onPin = vi.fn();
    const onClose = vi.fn();
    render(<ContextMenu items={makeItems(onPin)} position={POS} onClose={onClose} letterShortcuts />);
    fireEvent.keyDown(document, { key: 'p' });
    expect(onPin).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('letter opens a submenu, then letters retarget inside it', () => {
    const onSub = vi.fn();
    const onClose = vi.fn();
    render(<ContextMenu items={makeItems(vi.fn(), onSub)} position={POS} onClose={onClose} letterShortcuts />);
    fireEvent.keyDown(document, { key: 'o' });   // opens "Open in"
    expect(onClose).not.toHaveBeenCalled();
    expect(screen.getByText('Kiro')).toBeTruthy();
    fireEvent.keyDown(document, { key: 'k' });   // fires "Kiro" in the submenu
    expect(onSub).toHaveBeenCalledOnce();
    expect(onClose).toHaveBeenCalledOnce();
  });

  it('on a first-letter collision the earlier item wins and the later shows no keycap', () => {
    const onCopy = vi.fn();
    const onCut = vi.fn();
    const items: MenuItem[] = [
      { label: 'Copy', onClick: onCopy },
      { label: 'Cut', onClick: onCut },
      { label: 'Paste', onClick: vi.fn() },
    ];
    render(<ContextMenu items={items} position={POS} onClose={vi.fn()} letterShortcuts />);
    const caps = document.querySelectorAll('.ws-ctx-keycap');
    expect(Array.from(caps).map((c) => c.textContent)).toEqual(['C', 'P']);
    fireEvent.keyDown(document, { key: 'c' });
    expect(onCopy).toHaveBeenCalledOnce();
    expect(onCut).not.toHaveBeenCalled();
  });

  it('letters are inert without letterShortcuts', () => {
    const onPin = vi.fn();
    render(<ContextMenu items={makeItems(onPin)} position={POS} onClose={vi.fn()} />);
    fireEvent.keyDown(document, { key: 'p' });
    expect(onPin).not.toHaveBeenCalled();
    expect(document.querySelectorAll('.ws-ctx-keycap')).toHaveLength(0);
  });

  it('letters matching no label do nothing', () => {
    const onClose = vi.fn();
    render(<ContextMenu items={makeItems()} position={POS} onClose={onClose} letterShortcuts />);
    fireEvent.keyDown(document, { key: 'z' });
    expect(onClose).not.toHaveBeenCalled();
  });

  it('letters with a modifier are ignored (no OS-chord hijack)', () => {
    const onPin = vi.fn();
    const onClose = vi.fn();
    render(<ContextMenu items={makeItems(onPin)} position={POS} onClose={onClose} letterShortcuts />);
    fireEvent.keyDown(document, { key: 'p', metaKey: true });
    expect(onPin).not.toHaveBeenCalled();
    expect(onClose).not.toHaveBeenCalled();
  });

  it('renders into document.body via a portal, escaping ancestor stacking', () => {
    // The menu is portaled so its z-index isn't trapped inside the sidebar's
    // stacking context (position: relative; z-index: 10). Assert the root
    // lands directly under <body>, not the render container.
    const { container } = render(
      <ContextMenu items={makeItems()} position={POS} onClose={vi.fn()} letterShortcuts />,
    );
    const menu = document.querySelector('.ws-context-menu');
    expect(menu).toBeTruthy();
    expect(menu?.parentElement).toBe(document.body);
    expect(container.querySelector('.ws-context-menu')).toBeNull();
  });
});
