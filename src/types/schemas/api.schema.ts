import { z } from 'zod';

/** Schema for JSON error bodies returned by the backend. */
export const ErrorResponseSchema = z.object({
  detail: z.string().optional(),
  message: z.string().optional(),
  error: z.string().optional(),
}).passthrough();
