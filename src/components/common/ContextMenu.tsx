import React, { useEffect, useRef, useCallback, useState } from 'react';
import { createPortal } from 'react-dom';
import { useDismiss } from '@/hooks/useDismiss';

/** First-letter keycaps: each actionable item claims the first letter of its
 *  label (Pin -> P, Rename -> R). First occurrence wins — a later item whose
 *  letter is already taken gets no keycap, and its letter fires the earlier
 *  item. Returns index -> lowercase letter for the items that own one. */
const assignLetters = (items: MenuItem[]): Map<number, string> => {
  const used = new Set<string>();
  const map = new Map<number, string>();
  items.forEach((item, i) => {
    if (item.separator) return;
    const m = item.label.match(/[a-z]/i);
    if (!m) return;
    const letter = m[0].toLowerCase();
    if (used.has(letter)) return;
    used.add(letter);
    map.set(i, letter);
  });
  return map;
};

export interface MenuItem {
  label: string;
  icon?: string;
  shortcut?: string;
  onClick?: () => void;
  disabled?: boolean;
  danger?: boolean;
  separator?: boolean;
  children?: MenuItem[];
  /** When false, the menu stays open after clicking this item. Default: true. */
  closeOnClick?: boolean;
}

export interface ContextMenuProps {
  items: MenuItem[];
  position: { x: number; y: number };
  onClose: () => void;
  /** Extra class on the menu root. Used to opt into variants like the
   *  compact macOS/VS Code-style menu without restyling every consumer. */
  className?: string;
  /** Show each item's first label letter as a keycap badge (Pin -> P);
   *  pressing the letter fires the item (or opens its submenu, retargeting
   *  letters to it). Derived from labels at render — consumers never maintain
   *  them; on a first-letter collision the earlier item wins and the later one
   *  shows no keycap. Mutually exclusive with per-item `shortcut` hints. */
  letterShortcuts?: boolean;
}

/**
 * VS Code-style context menu matching the original ws-context-menu.
 * Uses the ws-ctx-* CSS classes from modules/context-menu.css.
 * Supports keyboard navigation, submenus, danger items, shortcuts.
 */
export const ContextMenu: React.FC<ContextMenuProps> = ({ items, position, onClose, className, letterShortcuts }) => {
  const menuRef = useRef<HTMLDivElement>(null);
  const [activeIndex, setActiveIndex] = useState(-1);
  const [submenuIndex, setSubmenuIndex] = useState(-1);
  const [adjustedPos, setAdjustedPos] = useState(position);

  // Get actionable (non-separator) item indices
  const actionableIndices = items.reduce<number[]>((acc, item, i) => {
    if (!item.separator) acc.push(i);
    return acc;
  }, []);

  // First-letter keycap assignment (index -> letter), collision-free.
  const letterByIndex = assignLetters(items);

  // Viewport-aware repositioning
  useEffect(() => {
    requestAnimationFrame(() => {
      const el = menuRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      let { x, y } = position;
      if (rect.right > window.innerWidth) x = window.innerWidth - rect.width - 4;
      if (rect.bottom > window.innerHeight) y = window.innerHeight - rect.height - 4;
      if (x < 0) x = 4;
      if (y < 0) y = 4;
      setAdjustedPos({ x, y });
    });
  }, [position]);

  // Outside-click dismiss via the shared hook. Escape lives in the keyboard
  // handler below (alongside arrow/enter nav), so disable it here to avoid a
  // double binding. A right-click anywhere also dismisses.
  useDismiss(menuRef, onClose, { escape: false });
  useEffect(() => {
    const handleContextMenu = () => onClose();
    document.addEventListener('contextmenu', handleContextMenu);
    return () => document.removeEventListener('contextmenu', handleContextMenu);
  }, [onClose]);

  // Keyboard navigation
  const handleKeyDown = useCallback(
    (e: KeyboardEvent) => {
      switch (e.key) {
        case 'Escape':
          e.preventDefault();
          onClose();
          break;
        case 'ArrowDown': {
          e.preventDefault();
          const curPos = actionableIndices.indexOf(activeIndex);
          const next = curPos < actionableIndices.length - 1 ? curPos + 1 : 0;
          setActiveIndex(actionableIndices[next]);
          setSubmenuIndex(-1);
          break;
        }
        case 'ArrowUp': {
          e.preventDefault();
          const curPos = actionableIndices.indexOf(activeIndex);
          const prev = curPos > 0 ? curPos - 1 : actionableIndices.length - 1;
          setActiveIndex(actionableIndices[prev]);
          setSubmenuIndex(-1);
          break;
        }
        case 'ArrowRight': {
          e.preventDefault();
          const item = items[activeIndex];
          if (item?.children?.length) {
            setSubmenuIndex(activeIndex);
          }
          break;
        }
        case 'ArrowLeft':
          e.preventDefault();
          setSubmenuIndex(-1);
          break;
        case 'Enter': {
          e.preventDefault();
          const item = items[activeIndex];
          if (item && !item.disabled && !item.separator) {
            if (item.children?.length) {
              setSubmenuIndex(activeIndex);
            } else if (item.onClick) {
              item.onClick();
              if (item.closeOnClick !== false) onClose();
            }
          }
          break;
        }
        default: {
          // First-letter shortcuts: target the open submenu when there is
          // one, otherwise the top level. Ignore letters pressed with a
          // modifier so OS/browser chords (⌘A, Ctrl+C, …) aren't hijacked.
          if (!letterShortcuts || e.ctrlKey || e.metaKey || e.altKey) break;
          if (!/^[a-z]$/i.test(e.key)) break;
          e.preventDefault();
          const letter = e.key.toLowerCase();
          const findByLetter = (map: Map<number, string>): number | undefined => {
            for (const [i, l] of map) if (l === letter) return i;
            return undefined;
          };
          if (submenuIndex >= 0) {
            const children = items[submenuIndex]?.children ?? [];
            const ci = findByLetter(assignLetters(children));
            const target = ci !== undefined ? children[ci] : undefined;
            if (target && !target.disabled && target.onClick) {
              target.onClick();
              onClose();
            }
          } else {
            const idx = findByLetter(letterByIndex);
            const item = idx !== undefined ? items[idx] : undefined;
            if (item && idx !== undefined && !item.disabled) {
              if (item.children?.length) {
                setActiveIndex(idx);
                setSubmenuIndex(idx);
              } else if (item.onClick) {
                item.onClick();
                if (item.closeOnClick !== false) onClose();
              }
            }
          }
          break;
        }
      }
    },
    [activeIndex, actionableIndices, letterByIndex, items, onClose, letterShortcuts, submenuIndex],
  );

  useEffect(() => {
    document.addEventListener('keydown', handleKeyDown);
    return () => document.removeEventListener('keydown', handleKeyDown);
  }, [handleKeyDown]);

  // Portal to <body> so the fixed menu escapes any ancestor stacking context
  // (e.g. the sidebar's `position: relative; z-index: 10`). Without this the
  // menu's z-index is trapped below sibling panels and its submenu renders
  // behind the workspace/chat divider.
  return createPortal(
    <div
      ref={menuRef}
      className={`ws-context-menu${className ? ` ${className}` : ''}`}
      style={{ position: 'fixed', top: adjustedPos.y, left: adjustedPos.x }}
      role="menu"
    >
      {items.map((item, index) => {
        if (item.separator) {
          return <div key={index} className="ws-ctx-separator" role="separator" />;
        }

        const isActive = index === activeIndex;
        const hasChildren = Boolean(item.children?.length);
        const showSubmenu = hasChildren && submenuIndex === index;

        const classNames = [
          'ws-ctx-item',
          isActive ? 'ws-ctx-active' : '',
          item.danger ? 'ws-ctx-danger' : '',
          item.disabled ? 'ws-ctx-disabled' : '',
        ].filter(Boolean).join(' ');

        return (
          <div
            key={index}
            className={classNames}
            role="menuitem"
            aria-disabled={item.disabled}
            onClick={() => {
              if (item.disabled) return;
              if (hasChildren) {
                setSubmenuIndex(index);
                return;
              }
              if (item.onClick) {
                item.onClick();
                if (item.closeOnClick !== false) onClose();
              }
            }}
            onMouseEnter={() => {
              setActiveIndex(index);
              if (hasChildren) setSubmenuIndex(index);
              else setSubmenuIndex(-1);
            }}
          >
            <span className="ws-ctx-icon">{item.icon ?? ''}</span>
            <span className="ws-ctx-label">{item.label}</span>
            {letterShortcuts && letterByIndex.has(index) ? (
              <span className="ws-ctx-keycap">{letterByIndex.get(index)?.toUpperCase()}</span>
            ) : (
              item.shortcut && <span className="ws-ctx-shortcut">{item.shortcut}</span>
            )}
            {hasChildren && <span className="ws-ctx-submenu-arrow">▸</span>}
            {showSubmenu && (
              <Submenu
                items={item.children ?? []}
                onClose={onClose}
                parentRef={menuRef}
                className={className}
                letterShortcuts={letterShortcuts}
              />
            )}
          </div>
        );
      })}
    </div>,
    document.body,
  );
};

/** Submenu rendered within a parent item, viewport-aware. */
const Submenu: React.FC<{
  items: MenuItem[];
  onClose: () => void;
  parentRef: React.RefObject<HTMLDivElement | null>;
  className?: string;
  letterShortcuts?: boolean;
}> = ({ items, onClose, className, letterShortcuts }) => {
  const subRef = useRef<HTMLDivElement>(null);
  const [offset, setOffset] = useState({ left: '100%', top: '0px' });

  useEffect(() => {
    requestAnimationFrame(() => {
      const el = subRef.current;
      if (!el) return;
      const rect = el.getBoundingClientRect();
      const newOffset = { left: '100%', top: '0px' };
      if (rect.right > window.innerWidth) newOffset.left = `-${rect.width}px`;
      if (rect.bottom > window.innerHeight) newOffset.top = `-${rect.height - 28}px`;
      setOffset(newOffset);
    });
  }, []);

  return (
    <div
      ref={subRef}
      className={`ws-context-menu ws-ctx-submenu${className ? ` ${className}` : ''}`}
      style={{ position: 'absolute', left: offset.left, top: offset.top }}
      role="menu"
    >
      {(() => {
        const subLetters = assignLetters(items);
        return items.map((item, i) => {
          if (item.separator) {
            return <div key={i} className="ws-ctx-separator" role="separator" />;
          }
          const classNames = [
            'ws-ctx-item',
            item.danger ? 'ws-ctx-danger' : '',
            item.disabled ? 'ws-ctx-disabled' : '',
          ].filter(Boolean).join(' ');

          return (
            <div
              key={i}
              className={classNames}
              role="menuitem"
              onClick={() => {
                if (item.disabled) return;
                if (item.onClick) {
                  item.onClick();
                  onClose();
                }
              }}
            >
              <span className="ws-ctx-icon">{item.icon ?? ''}</span>
              <span className="ws-ctx-label">{item.label}</span>
              {letterShortcuts && subLetters.has(i) ? (
                <span className="ws-ctx-keycap">{subLetters.get(i)?.toUpperCase()}</span>
              ) : (
                item.shortcut && <span className="ws-ctx-shortcut">{item.shortcut}</span>
              )}
            </div>
          );
        });
      })()}
    </div>
  );
};
