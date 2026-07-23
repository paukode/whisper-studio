import { useCallback, useEffect, useState } from 'react';
import type { ZodType } from 'zod';

/**
 * Typed localStorage sync hook.
 *
 * Reads the initial value from localStorage (falling back to `defaultValue`),
 * and writes back to localStorage whenever the value changes.
 *
 * @param key - The localStorage key
 * @param defaultValue - Fallback value when key is not present or parsing fails
 * @param schema - Optional Zod schema to validate parsed values
 * @returns A tuple of [value, setValue] similar to useState
 */
export function useLocalStorage<T>(
  key: string,
  defaultValue: T,
  schema?: ZodType<T>,
): [T, (value: T | ((prev: T) => T)) => void] {
  const [storedValue, setStoredValue] = useState<T>(() => {
    try {
      const item = localStorage.getItem(key);
      if (item === null) return defaultValue;
      const raw: unknown = JSON.parse(item);
      if (schema) {
        const result = schema.safeParse(raw);
        return result.success ? result.data : defaultValue;
      }
      return raw as T;
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
        } catch {
          // localStorage may be unavailable (quota exceeded, private browsing)
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
        const raw: unknown = JSON.parse(event.newValue);
        if (schema) {
          const result = schema.safeParse(raw);
          setStoredValue(result.success ? result.data : defaultValue);
          return;
        }
        setStoredValue(raw as T);
        return;
      } catch {
        setStoredValue(defaultValue);
      }
    };

    window.addEventListener('storage', handleStorageChange);
    return () => {
      window.removeEventListener('storage', handleStorageChange);
    };
  }, [key, defaultValue, schema]);

  return [storedValue, setValue];
}
