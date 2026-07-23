export type ThemeKey =
  | 'auto'
  | 'dark'
  | 'light'
  | 'dark-high-contrast'
  | 'light-high-contrast'
  | 'dark-daltonized'
  | 'light-daltonized'
  | 'dark-taw'
  | 'light-taw';

export interface ThemeOption {
  key: ThemeKey;
  label: string;
}

export interface ThemeContextValue {
  themeKey: ThemeKey;
  resolvedTheme: string;
  setTheme: (key: ThemeKey) => void;
  themes: readonly ThemeOption[];
}
