import { useEffect } from 'react';

/**
 * Parse a key combo string like "Cmd+Shift+K" into its parts.
 * Supports: Ctrl, Alt, Shift, Meta (or Cmd, Command, Mod) modifiers.
 */
interface ParsedCombo {
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
  key: string;
}

function parseKeyCombo(combo: string): ParsedCombo {
  const parts = combo.split('+').map((p) => p.trim());
  const modifiers: ParsedCombo = {
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    metaKey: false,
    key: '',
  };

  for (const part of parts) {
    const lower = part.toLowerCase();
    switch (lower) {
      case 'ctrl':
      case 'control':
        modifiers.ctrlKey = true;
        break;
      case 'alt':
        modifiers.altKey = true;
        break;
      case 'shift':
        modifiers.shiftKey = true;
        break;
      case 'meta':
      case 'cmd':
      case 'command':
      case 'mod':
        modifiers.metaKey = true;
        break;
      default:
        modifiers.key = lower;
        break;
    }
  }

  return modifiers;
}

/**
 * Register a keyboard shortcut.
 *
 * @param combo - Key combo string, e.g. "Cmd+K", "Cmd+Shift+P", "Meta+S"
 * @param handler - Callback invoked when the shortcut is pressed
 * @param enabled - Whether the shortcut is active (default: true)
 *
 * Returns a cleanup function via useEffect.
 */
export function useKeyboardShortcut(
  combo: string,
  handler: (event: KeyboardEvent) => void,
  enabled = true,
): void {
  useEffect(() => {
    if (!enabled || !combo) return;

    const parsed = parseKeyCombo(combo);

    const handleKeyDown = (event: KeyboardEvent) => {
      // Convention: any handler that consumes a key (overlay dismissers,
      // autocomplete) calls preventDefault; a consumed key is not a shortcut.
      // This listener is on window (bubble phase), so element/document-level
      // handlers have always run first.
      if (event.defaultPrevented) return;
      // Never treat IME composition keys as shortcuts.
      if (event.isComposing) return;
      const keyMatches = event.key.toLowerCase() === parsed.key;
      const ctrlMatches = event.ctrlKey === parsed.ctrlKey;
      const metaMatches = event.metaKey === parsed.metaKey;
      const altMatches = event.altKey === parsed.altKey;
      const shiftMatches = event.shiftKey === parsed.shiftKey;

      if (
        keyMatches &&
        ctrlMatches &&
        metaMatches &&
        altMatches &&
        shiftMatches
      ) {
        event.preventDefault();
        handler(event);
      }
    };

    window.addEventListener('keydown', handleKeyDown);
    return () => {
      window.removeEventListener('keydown', handleKeyDown);
    };
  }, [combo, handler, enabled]);
}
