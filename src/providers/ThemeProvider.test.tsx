import { render, screen, act, fireEvent } from '@testing-library/react';
import { describe, it, expect, beforeEach, vi } from 'vitest';
import { ThemeProvider, useTheme } from './ThemeProvider';
import { STORAGE_KEYS } from '@/utils/storageKeys';

/** Helper component that exposes theme context values for testing. */
function ThemeConsumer() {
  const { themeKey, resolvedTheme, setTheme } = useTheme();
  return (
    <div>
      <span data-testid="theme-key">{themeKey}</span>
      <span data-testid="resolved-theme">{resolvedTheme}</span>
      <button data-testid="set-light" onClick={() => setTheme('light')}>Light</button>
      <button data-testid="set-dark" onClick={() => setTheme('dark')}>Dark</button>
      <button data-testid="set-auto" onClick={() => setTheme('auto')}>Auto</button>
    </div>
  );
}

describe('ThemeProvider', () => {
  beforeEach(() => {
    localStorage.clear();
    document.documentElement.removeAttribute('data-theme');
  });

  it('follows the system when localStorage is empty', () => {
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('theme-key').textContent).toBe('auto');
  });

  it('resolves auto to dark when system prefers dark', () => {
    // The matchMedia mock defaults to matches: false for prefers-color-scheme: light,
    // meaning the system prefers dark.
    localStorage.setItem(STORAGE_KEYS.THEME, 'auto');
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('resolved-theme').textContent).toBe('dark');
  });

  it('reads persisted theme from localStorage', () => {
    localStorage.setItem(STORAGE_KEYS.THEME, 'light');

    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('theme-key').textContent).toBe('light');
    expect(screen.getByTestId('resolved-theme').textContent).toBe('light');
  });

  it('ignores invalid localStorage values and follows the system', () => {
    localStorage.setItem(STORAGE_KEYS.THEME, 'neon-pink');

    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('theme-key').textContent).toBe('auto');
  });

  it.each([
    ['dark-high-contrast', 'dark'],
    ['dark-daltonized', 'dark'],
    ['dark-taw', 'dark'],
    ['light-high-contrast', 'light'],
    ['light-daltonized', 'light'],
    ['light-taw', 'light'],
  ])('keeps the mode of a theme saved before the merge (%s)', (stored, mode) => {
    localStorage.setItem(STORAGE_KEYS.THEME, stored);

    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('theme-key').textContent).toBe(mode);
    expect(document.documentElement.getAttribute('data-theme')).toBe(mode);
  });

  it('persists theme to localStorage when setTheme is called', () => {
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByTestId('set-light'));

    expect(localStorage.getItem(STORAGE_KEYS.THEME)).toBe('light');
    expect(screen.getByTestId('theme-key').textContent).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('updates data-theme when switching themes', () => {
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    fireEvent.click(screen.getByTestId('set-dark'));
    expect(document.documentElement.getAttribute('data-theme')).toBe('dark');

    fireEvent.click(screen.getByTestId('set-light'));
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');
  });

  it('offers system, light and dark, and resolves every option to a mode', () => {
    function Options() {
      const { themes, setTheme, resolvedTheme } = useTheme();
      return (
        <div>
          {themes.map((t) => (
            <button key={t.key} data-testid={`opt-${t.key}`} onClick={() => setTheme(t.key)}>{t.label}</button>
          ))}
          <span data-testid="mode">{resolvedTheme}</span>
        </div>
      );
    }
    render(
      <ThemeProvider>
        <Options />
      </ThemeProvider>,
    );

    for (const key of ['auto', 'light', 'dark']) {
      fireEvent.click(screen.getByTestId(`opt-${key}`));
      expect(['light', 'dark']).toContain(screen.getByTestId('mode').textContent);
      expect(document.documentElement.getAttribute('data-theme')).toBe(screen.getByTestId('mode').textContent);
    }
  });

  it('resolves auto to system preference when matchMedia changes', () => {
    let changeHandler: ((e: MediaQueryListEvent) => void) | null = null;

    const mqlMock = {
      matches: false,
      media: '(prefers-color-scheme: light)',
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn((_event: string, handler: (e: MediaQueryListEvent) => void) => {
        changeHandler = handler;
      }),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(() => true),
    };

    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn(() => mqlMock as unknown as MediaQueryList);

    localStorage.setItem(STORAGE_KEYS.THEME, 'auto');
    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    expect(screen.getByTestId('resolved-theme').textContent).toBe('dark');

    // Simulate system switching to light
    act(() => {
      changeHandler?.({ matches: true } as MediaQueryListEvent);
    });

    expect(screen.getByTestId('resolved-theme').textContent).toBe('light');
    expect(document.documentElement.getAttribute('data-theme')).toBe('light');

    window.matchMedia = originalMatchMedia;
  });

  it('does not change resolved theme on system change when not in auto mode', () => {
    let changeHandler: ((e: MediaQueryListEvent) => void) | null = null;

    const mqlMock = {
      matches: false,
      media: '(prefers-color-scheme: light)',
      onchange: null,
      addListener: vi.fn(),
      removeListener: vi.fn(),
      addEventListener: vi.fn((_event: string, handler: (e: MediaQueryListEvent) => void) => {
        changeHandler = handler;
      }),
      removeEventListener: vi.fn(),
      dispatchEvent: vi.fn(() => true),
    };

    const originalMatchMedia = window.matchMedia;
    window.matchMedia = vi.fn(() => mqlMock as unknown as MediaQueryList);

    render(
      <ThemeProvider>
        <ThemeConsumer />
      </ThemeProvider>,
    );

    // Switch to explicit light theme
    fireEvent.click(screen.getByTestId('set-light'));
    expect(screen.getByTestId('resolved-theme').textContent).toBe('light');

    // Simulate system switching to dark — should not affect resolved theme
    act(() => {
      changeHandler?.({ matches: false } as MediaQueryListEvent);
    });

    expect(screen.getByTestId('resolved-theme').textContent).toBe('light');

    window.matchMedia = originalMatchMedia;
  });
});

describe('useTheme', () => {
  it('throws when used outside ThemeProvider', () => {
    const consoleSpy = vi.spyOn(console, 'error').mockImplementation(() => {});

    function BadConsumer() {
      useTheme();
      return null;
    }

    expect(() => render(<BadConsumer />)).toThrow(
      'useTheme must be used within a ThemeProvider',
    );

    consoleSpy.mockRestore();
  });
});
