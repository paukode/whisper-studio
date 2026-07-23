import React, { createContext, useCallback, useContext, useEffect, useMemo, useState } from 'react';
import type { ThemeContextValue, ThemeKey, ThemeOption } from '@/types/theme';
import { STORAGE_KEYS } from '@/utils/storageKeys';

/**
 * All supported theme options with human-readable labels.
 */
const THEME_OPTIONS: readonly ThemeOption[] = [
  { key: 'auto', label: 'Auto (System)' },
  { key: 'dark', label: 'Dark' },
  { key: 'light', label: 'Light' },
  { key: 'dark-high-contrast', label: 'Dark High Contrast' },
  { key: 'light-high-contrast', label: 'Light High Contrast' },
  { key: 'dark-daltonized', label: 'Dark (Color-blind)' },
  { key: 'light-daltonized', label: 'Light (Color-blind)' },
  { key: 'dark-taw', label: 'Taw Dark' },
  { key: 'light-taw', label: 'Taw Light' },
] as const;

const VALID_THEME_KEYS = new Set<string>(THEME_OPTIONS.map((t) => t.key));

/**
 * Reads the persisted theme key from localStorage.
 * Falls back to the Taw Light theme if the stored value is missing or invalid.
 */
function readStoredThemeKey(): ThemeKey {
  try {
    const stored = localStorage.getItem(STORAGE_KEYS.THEME);
    if (stored && VALID_THEME_KEYS.has(stored)) {
      return stored as ThemeKey;
    }
  } catch {
    // localStorage may be unavailable (e.g. in some test environments)
  }
  return 'light-taw';
}

/**
 * Detects the system color-scheme preference via matchMedia.
 * Returns 'light' or 'dark'.
 */
function getSystemTheme(): 'light' | 'dark' {
  if (typeof window !== 'undefined' && window.matchMedia) {
    return window.matchMedia('(prefers-color-scheme: light)').matches ? 'light' : 'dark';
  }
  return 'dark';
}

/**
 * Resolves the actual theme string to apply.
 * When the key is 'auto', it resolves to the system preference.
 */
function resolveTheme(key: ThemeKey, systemTheme: 'light' | 'dark'): string {
  return key === 'auto' ? systemTheme : key;
}

export const ThemeContext = createContext<ThemeContextValue | null>(null);

/**
 * ThemeProvider manages the active theme for the application.
 *
 * - Persists the selected theme key to localStorage (`whisper_theme`)
 * - Sets the `data-theme` attribute on `document.documentElement`
 * - Listens to `matchMedia('(prefers-color-scheme: light)')` to reactively
 *   update the resolved theme when the key is 'auto'
 * - Exposes the current theme key, resolved theme, setter, and theme list
 *   via React context
 */
export const ThemeProvider: React.FC<{ children: React.ReactNode }> = ({ children }) => {
  const [themeKey, setThemeKey] = useState<ThemeKey>(readStoredThemeKey);
  const [systemTheme, setSystemTheme] = useState<'light' | 'dark'>(getSystemTheme);

  const resolvedTheme = useMemo(() => resolveTheme(themeKey, systemTheme), [themeKey, systemTheme]);

  // Apply data-theme attribute whenever the resolved theme changes
  useEffect(() => {
    document.documentElement.setAttribute('data-theme', resolvedTheme);
  }, [resolvedTheme]);

  // Listen for system color-scheme changes
  useEffect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;

    const mql = window.matchMedia('(prefers-color-scheme: light)');

    const handler = (e: MediaQueryListEvent) => {
      setSystemTheme(e.matches ? 'light' : 'dark');
    };

    mql.addEventListener('change', handler);
    return () => mql.removeEventListener('change', handler);
  }, []);

  const setTheme = useCallback((key: ThemeKey) => {
    setThemeKey(key);
    try {
      localStorage.setItem(STORAGE_KEYS.THEME, key);
    } catch {
      // localStorage may be unavailable
    }
  }, []);

  const value = useMemo<ThemeContextValue>(
    () => ({
      themeKey,
      resolvedTheme,
      setTheme,
      themes: THEME_OPTIONS,
    }),
    [themeKey, resolvedTheme, setTheme],
  );

  return <ThemeContext.Provider value={value}>{children}</ThemeContext.Provider>;
};

/**
 * Hook to access the current theme context.
 * Must be used within a ThemeProvider.
 */
export function useTheme(): ThemeContextValue {
  const ctx = useContext(ThemeContext);
  if (!ctx) {
    throw new Error('useTheme must be used within a ThemeProvider');
  }
  return ctx;
}
