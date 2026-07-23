import { useCallback, useState } from 'react';
import { ApiError } from '@/types/api';
import * as client from '@/api/client';

export interface UseApiReturn {
  get: <T>(url: string) => Promise<T>;
  post: <T>(url: string, body?: unknown) => Promise<T>;
  put: <T>(url: string, body?: unknown) => Promise<T>;
  del: <T>(url: string) => Promise<T>;
  isLoading: boolean;
  error: ApiError | null;
}

export function useApi(): UseApiReturn {
  const [isLoading, setIsLoading] = useState(false);
  const [error, setError] = useState<ApiError | null>(null);

  const wrap = useCallback(
    async <T>(fn: () => Promise<T>): Promise<T> => {
      setIsLoading(true);
      setError(null);
      try {
        const result = await fn();
        return result;
      } catch (err: unknown) {
        const apiError =
          err instanceof ApiError
            ? err
            : new ApiError(0, err instanceof Error ? err.message : 'Unknown error', '', '');
        setError(apiError);
        throw apiError;
      } finally {
        setIsLoading(false);
      }
    },
    [],
  );

  const get = useCallback(
    <T>(url: string): Promise<T> => wrap(() => client.get<T>(url)),
    [wrap],
  );

  const post = useCallback(
    <T>(url: string, body?: unknown): Promise<T> => wrap(() => client.post<T>(url, body)),
    [wrap],
  );

  const put = useCallback(
    <T>(url: string, body?: unknown): Promise<T> => wrap(() => client.put<T>(url, body)),
    [wrap],
  );

  const del = useCallback(
    <T>(url: string): Promise<T> => wrap(() => client.del<T>(url)),
    [wrap],
  );

  return { get, post, put, del, isLoading, error };
}
