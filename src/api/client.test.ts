import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { get, post, put, del } from './client';
import { ApiError } from '@/types/api';

// Helper to create a mock fetch that returns a fresh Response each time
function mockFetch(
  body: unknown,
  init: { status?: number; statusText?: string; headers?: Record<string, string> } = {},
): void {
  vi.mocked(globalThis.fetch).mockImplementation(() => {
    const { status = 200, statusText = 'OK', headers = {} } = init;
    const isNullBody = status === 204 || status === 304;
    const isJson = typeof body === 'object' && body !== null;
    const text = isNullBody ? null : isJson ? JSON.stringify(body) : String(body ?? '');
    const responseHeaders = new Headers(headers);
    if (isJson && !responseHeaders.has('content-type')) {
      responseHeaders.set('content-type', 'application/json');
    }
    return Promise.resolve(
      new Response(text, { status, statusText, headers: responseHeaders }),
    );
  });
}

describe('API client', () => {
  const originalFetch = globalThis.fetch;

  beforeEach(() => {
    globalThis.fetch = vi.fn();
  });

  afterEach(() => {
    globalThis.fetch = originalFetch;
  });

  describe('get', () => {
    it('sends a GET request and returns JSON', async () => {
      const data = { id: 1, name: 'test' };
      mockFetch(data);

      const result = await get<{ id: number; name: string }>('/api/items/1');

      expect(globalThis.fetch).toHaveBeenCalledWith('/api/items/1', {
        method: 'GET',
        headers: {},
        body: undefined,
        signal: undefined,
      });
      expect(result).toEqual(data);
    });

    it('returns text when content-type is not JSON', async () => {
      mockFetch('plain text', { headers: { 'content-type': 'text/plain' } });

      const result = await get<string>('/api/text');
      expect(result).toBe('plain text');
    });

    it('passes signal for request cancellation', async () => {
      const controller = new AbortController();
      mockFetch({ ok: true });

      await get('/api/items', { signal: controller.signal });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/api/items',
        expect.objectContaining({ signal: controller.signal }),
      );
    });

    it('passes custom headers', async () => {
      mockFetch({ ok: true });

      await get('/api/items', { headers: { 'X-Custom': 'value' } });

      expect(globalThis.fetch).toHaveBeenCalledWith(
        '/api/items',
        expect.objectContaining({
          headers: { 'X-Custom': 'value' },
        }),
      );
    });
  });

  describe('post', () => {
    it('sends a POST request with JSON body', async () => {
      const body = { name: 'new item' };
      const response = { id: 2, name: 'new item' };
      mockFetch(response);

      const result = await post<{ id: number; name: string }>('/api/items', body);

      expect(globalThis.fetch).toHaveBeenCalledWith('/api/items', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: undefined,
      });
      expect(result).toEqual(response);
    });

    it('sends a POST request without body', async () => {
      mockFetch({ ok: true });

      await post('/api/action');

      expect(globalThis.fetch).toHaveBeenCalledWith('/api/action', {
        method: 'POST',
        headers: {},
        body: undefined,
        signal: undefined,
      });
    });
  });

  describe('put', () => {
    it('sends a PUT request with JSON body', async () => {
      const body = { name: 'updated' };
      mockFetch({ ok: true });

      await put('/api/items/1', body);

      expect(globalThis.fetch).toHaveBeenCalledWith('/api/items/1', {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body),
        signal: undefined,
      });
    });
  });

  describe('del', () => {
    it('sends a DELETE request and handles 204 No Content', async () => {
      mockFetch(null, { status: 204, statusText: 'No Content' });

      const result = await del('/api/items/1');

      expect(globalThis.fetch).toHaveBeenCalledWith('/api/items/1', {
        method: 'DELETE',
        headers: {},
        body: undefined,
        signal: undefined,
      });
      expect(result).toBeUndefined();
    });

    it('sends a DELETE request and returns JSON when body present', async () => {
      mockFetch({ deleted: true });

      const result = await del<{ deleted: boolean }>('/api/items/1');
      expect(result).toEqual({ deleted: true });
    });
  });

  describe('error handling', () => {
    it('throws ApiError for 400 Bad Request with JSON detail', async () => {
      mockFetch({ detail: 'Invalid input' }, { status: 400, statusText: 'Bad Request' });

      try {
        await get('/api/items');
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(400);
        expect(err.message).toBe('Invalid input');
        expect(err.url).toBe('/api/items');
        expect(err.method).toBe('GET');
      }
    });

    it('throws ApiError for 404 Not Found with plain text body', async () => {
      mockFetch('Not Found', {
        status: 404,
        statusText: 'Not Found',
        headers: { 'content-type': 'text/plain' },
      });

      try {
        await get('/api/items/999');
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(404);
        expect(err.message).toBe('Not Found');
      }
    });

    it('throws ApiError for 500 Internal Server Error', async () => {
      mockFetch({ error: 'Something went wrong' }, { status: 500, statusText: 'Internal Server Error' });

      try {
        await post('/api/action', {});
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(500);
        expect(err.message).toBe('Something went wrong');
        expect(err.method).toBe('POST');
      }
    });

    it('throws ApiError with status 0 and a clear message for network errors', async () => {
      // fetch rejects with a TypeError when the server is unreachable; we map
      // that to an actionable message instead of the cryptic "Failed to fetch".
      vi.mocked(globalThis.fetch).mockRejectedValue(new TypeError('Failed to fetch'));

      try {
        await get('/api/items');
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(0);
        expect(err.message).toMatch(/can't reach the server/i);
        expect(err.url).toBe('/api/items');
        expect(err.method).toBe('GET');
      }
    });

    it('throws ApiError with "Request aborted" for AbortError', async () => {
      const abortError = new DOMException('The operation was aborted.', 'AbortError');
      vi.mocked(globalThis.fetch).mockRejectedValue(abortError);

      try {
        await get('/api/items');
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(0);
        expect(err.message).toBe('Request aborted');
      }
    });

    it('uses statusText when response body is empty', async () => {
      mockFetch('', { status: 502, statusText: 'Bad Gateway' });

      try {
        await get('/api/items');
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(502);
        expect(err.message).toBe('Bad Gateway');
      }
    });

    it('extracts message field from JSON error body', async () => {
      mockFetch({ message: 'Rate limited' }, { status: 429, statusText: 'Too Many Requests' });

      try {
        await get('/api/items');
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(429);
        expect(err.message).toBe('Rate limited');
      }
    });
  });

  describe('AbortController integration', () => {
    it('aborts an in-flight request', async () => {
      const controller = new AbortController();

      vi.mocked(globalThis.fetch).mockImplementation(
        (_url, init) =>
          new Promise((_resolve, reject) => {
            const signal = (init as RequestInit)?.signal;
            if (signal) {
              signal.addEventListener('abort', () => {
                reject(new DOMException('The operation was aborted.', 'AbortError'));
              });
            }
          }),
      );

      const promise = get('/api/slow', { signal: controller.signal });
      controller.abort();

      try {
        await promise;
        expect.fail('Should have thrown');
      } catch (e) {
        expect(e).toBeInstanceOf(ApiError);
        const err = e as ApiError;
        expect(err.status).toBe(0);
        expect(err.message).toBe('Request aborted');
      }
    });
  });
});
