import { useEffect } from 'react';

/**
 * Parse a key combo string like "Ctrl+Shift+K" into its parts.
 * Supports: Ctrl, Alt, Shift, Meta (or Cmd) modifiers.
 */
interface ParsedCombo {
  ctrlKey: boolean;
  altKey: boolean;
  shiftKey: boolean;
  metaKey: boolean;
  /** True for the cross-platform "mod" modifier (matches Cmd on macOS, Ctrl elsewhere). */
  modKey: boolean;
  key: string;
}

function parseKeyCombo(combo: string): ParsedCombo {
  const parts = combo.split('+').map((p) => p.trim());
  const modifiers: ParsedCombo = {
    ctrlKey: false,
    altKey: false,
    shiftKey: false,
    metaKey: false,
    modKey: false,
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
        modifiers.metaKey = true;
        break;
      case 'mod':
        modifiers.modKey = true;
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
 * @param combo - Key combo string, e.g. "Ctrl+K", "Ctrl+Shift+P", "Meta+S"
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
      // When `mod` is in the combo we accept either Cmd (macOS-typical) or
      // Ctrl, and we don't enforce the exact-match constraint for those two
      // keys so platform-native shortcuts work without per-OS branching.
      const ctrlMatches = parsed.modKey ? true : event.ctrlKey === parsed.ctrlKey;
      const metaMatches = parsed.modKey ? true : event.metaKey === parsed.metaKey;
      const modPressed = parsed.modKey ? (event.ctrlKey || event.metaKey) : true;
      const altMatches = event.altKey === parsed.altKey;
      const shiftMatches = event.shiftKey === parsed.shiftKey;

      if (
        keyMatches &&
        ctrlMatches &&
        metaMatches &&
        modPressed &&
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
