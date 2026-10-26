/** 'auto' follows the system setting and resolves to 'light' or 'dark'. */
export type ThemeKey = 'auto' | 'light' | 'dark';

export interface ThemeOption {
  key: ThemeKey;
  label: string;
}

export interface ThemeContextValue {
  themeKey: ThemeKey;
  resolvedTheme: 'light' | 'dark';
  setTheme: (key: ThemeKey) => void;
  themes: readonly ThemeOption[];
}
