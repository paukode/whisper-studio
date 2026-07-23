import type { ZodType } from 'zod';
import { ApiError } from '@/types/api';

export interface RequestOptions {
  signal?: AbortSignal;
  headers?: Record<string, string>;
  /** Optional Zod schema to validate the response. On failure, logs a warning and returns raw data. */
  schema?: ZodType;
}

async function parseErrorMessage(response: Response): Promise<string> {
  try {
    const body = await response.text();
    try {
      const json: Record<string, unknown> = JSON.parse(body);
      if (typeof json.detail === 'string') return json.detail;
      if (typeof json.message === 'string') return json.message;
      if (typeof json.error === 'string') return json.error;
    } catch {
      // Not JSON — use raw text
      if (body.length > 0) return body;
    }
  } catch {
    // Could not read body
  }
  return response.statusText || `HTTP ${response.status}`;
}

async function request<T>(
  method: string,
  url: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  const headers: Record<string, string> = {
    ...options?.headers,
  };

  let fetchBody: string | undefined;
  if (body !== undefined) {
    headers['Content-Type'] = headers['Content-Type'] ?? 'application/json';
    fetchBody = JSON.stringify(body);
  }

  let response: Response;
  try {
    response = await fetch(url, {
      method,
      headers,
      body: fetchBody,
      signal: options?.signal,
    });
  } catch (error: unknown) {
    // Network error or abort. fetch() rejects with a TypeError ("Failed to
    // fetch") when the server is unreachable — surface that as a clear,
    // actionable message instead of the cryptic browser default.
    const message =
      error instanceof DOMException && error.name === 'AbortError'
        ? 'Request aborted'
        : error instanceof TypeError
          ? "Can't reach the server. Make sure the app's backend is running, then try again."
          : error instanceof Error
            ? error.message
            : 'Network error';
    throw new ApiError(0, message, url, method);
  }

  if (!response.ok) {
    const message = await parseErrorMessage(response);
    throw new ApiError(response.status, message, url, method);
  }

  // Handle 204 No Content
  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get('content-type') ?? '';
  if (contentType.includes('application/json')) {
    const raw = await response.json();
    if (options?.schema) {
      const result = options.schema.safeParse(raw);
      if (result.success) return result.data as T;
      console.warn(`[api] Schema validation failed for ${method} ${url}:`, result.error);
    }
    return raw as T;
  }

  return (await response.text()) as T;
}

export async function get<T>(url: string, options?: RequestOptions): Promise<T> {
  return request<T>('GET', url, undefined, options);
}

export async function post<T>(
  url: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>('POST', url, body, options);
}

export async function put<T>(
  url: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>('PUT', url, body, options);
}

export async function del<T>(url: string, options?: RequestOptions): Promise<T> {
  return request<T>('DELETE', url, undefined, options);
}

export async function patch<T>(
  url: string,
  body?: unknown,
  options?: RequestOptions,
): Promise<T> {
  return request<T>('PATCH', url, body, options);
}
