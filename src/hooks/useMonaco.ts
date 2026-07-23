import { useCallback, useRef } from 'react';

/**
 * Monaco theme definition matching the Monaco editor's IStandaloneThemeData shape.
 * We use a lightweight inline type so we don't depend on the full Monaco types at
 * the hook level — the actual Monaco instance is managed by @monaco-editor/react.
 */
interface MonacoThemeData {
  base: 'vs' | 'vs-dark' | 'hc-black' | 'hc-light';
  inherit: boolean;
  rules: Array<{ token: string; foreground?: string; background?: string; fontStyle?: string }>;
  colors: Record<string, string>;
}

interface MonacoInstance {
  editor: {
    defineTheme: (name: string, data: MonacoThemeData) => void;
    setTheme: (name: string) => void;
  };
}

export interface UseMonacoReturn {
  /** Register one or more custom themes with the Monaco editor. */
  registerThemes: (themes: Record<string, MonacoThemeData>) => void;
  /** Switch the active Monaco editor theme by name. */
  setEditorTheme: (themeName: string) => void;
}

/**
 * Hook for managing Monaco editor theme registration and switching.
 *
 * The actual Monaco editor instance is managed by `@monaco-editor/react`.
 * This hook provides helpers for registering Whisper Studio themes
 * (whisper-dark, whisper-light, high-contrast variants) and switching
 * between them at runtime.
 */
export function useMonaco(): UseMonacoReturn {
  const monacoRef = useRef<MonacoInstance | null>(null);
  const registeredThemesRef = useRef<Set<string>>(new Set());

  const getMonaco = useCallback((): MonacoInstance | null => {
    if (monacoRef.current) return monacoRef.current;

    // Try to access the global monaco instance set by @monaco-editor/react
    const win = window as unknown as Record<string, unknown>;
    if (win.monaco && typeof win.monaco === 'object') {
      monacoRef.current = win.monaco as MonacoInstance;
      return monacoRef.current;
    }

    return null;
  }, []);

  const registerThemes = useCallback(
    (themes: Record<string, MonacoThemeData>) => {
      const monaco = getMonaco();
      if (!monaco) {
        // Monaco not loaded yet — store for later registration
        // In practice, callers should invoke this after Monaco is ready
        // (e.g., in the onMount callback of @monaco-editor/react)
        console.warn('useMonaco: Monaco not available yet. Themes not registered.');
        return;
      }

      for (const [name, data] of Object.entries(themes)) {
        if (!registeredThemesRef.current.has(name)) {
          monaco.editor.defineTheme(name, data);
          registeredThemesRef.current.add(name);
        }
      }
    },
    [getMonaco],
  );

  const setEditorTheme = useCallback(
    (themeName: string) => {
      const monaco = getMonaco();
      if (!monaco) {
        console.warn('useMonaco: Monaco not available yet. Cannot set theme.');
        return;
      }
      monaco.editor.setTheme(themeName);
    },
    [getMonaco],
  );

  return { registerThemes, setEditorTheme };
}
