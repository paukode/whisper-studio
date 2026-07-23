/**
 * Safely narrow an unknown caught value to an Error instance.
 * Handles: Error objects, strings, objects with .message, and fallback.
 */
export function toError(err: unknown): Error {
  if (err instanceof Error) return err;
  if (typeof err === 'string') return new Error(err);
  if (
    typeof err === 'object' &&
    err !== null &&
    'message' in err &&
    typeof (err as Record<string, unknown>).message === 'string'
  ) {
    return new Error((err as Record<string, unknown>).message as string);
  }
  return new Error(String(err));
}
