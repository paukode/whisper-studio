import { useCallback, useEffect, useState } from 'react';

/**
 * Typed localStorage sync hook.
 *
 * Reads the initial value from localStorage (falling back to `defaultValue`),
 * and writes back to localStorage whenever the value changes.
 *
 * @param key - The localStorage key
 * @param defaultValue - Fallback value when key is not present or parsing fails
 * @returns A tuple of [value, setValue] similar to useState
 */
export function useLocalStorage<T>(
  key: string,
  defaultValue: T,
): [T, (value: T | ((prev: T) => T)) => void] {
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = localStorage.getItem(key);
      if (item === null) return defaultValue;
      return JSON.parse(item) as T;
    } catch {
      return defaultValue;
    }
  });

  const setValue = useCallback(
    (value: T | ((prev: T) => T)) => {
      setStoredValue((prev) => {
        const nextValue = value instanceof Function ? value(prev) : value;
        try {
          localStorage.setItem(key, JSON.stringify(nextValue));
        } catch (err) {
          // Quota or a blocked store: the preference holds for this window
          // only. Not rethrown, this runs inside a React state updater.
          console.warn('[storage] could not save', key, err);
        }
        return nextValue;
      });
    },
    [key],
  );

  // Sync with storage events from other tabs/windows
  useEffect(() => {
    const handleStorageChange = (event: StorageEvent) => {
      if (event.key !== key) return;
      try {
        if (event.newValue === null) {
          setStoredValue(defaultValue);
          return;
        }
        setStoredValue(JSON.parse(event.newValue) as T);
      } catch {
        setStoredValue(defaultValue);
      }
    };

    window.addEventListener('storage', handleStorageChange);
    return () => {
      window.removeEventListener('storage', handleStorageChange);
    };
  }, [key, defaultValue]);

  return [storedValue, setValue];
}
