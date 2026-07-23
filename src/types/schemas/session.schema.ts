import { z } from 'zod';

export const SessionSummarySchema = z.object({
  id: z.string(),
  title: z.string().optional().default('Untitled'),
  date: z.string().optional().default(''),
  segmentCount: z.number().optional().default(0),
  chatCount: z.number().optional().default(0),
  workspacePath: z.string().optional().default(''),
  pinned: z.boolean().optional().default(false),
  archived: z.boolean().optional().default(false),
});

export const SessionListResponseSchema = z.array(SessionSummarySchema);

export type SessionSummaryParsed = z.infer<typeof SessionSummarySchema>;

/** GET /api/sessions/search: sessions whose message or transcript text
 *  matches, each with a one-line snippet around the first hit. */
export const SessionSearchResponseSchema = z.object({
  results: z.array(z.object({
    id: z.string(),
    snippet: z.string().optional().default(''),
  })).optional().default([]),
  /** True when the result cap cut the scan short: more matches may exist. */
  truncated: z.boolean().optional().default(false),
});

export type SessionSearchResponse = z.infer<typeof SessionSearchResponseSchema>;
