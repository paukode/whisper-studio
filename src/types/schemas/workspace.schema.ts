import { z } from 'zod';

export const FileTreeEntrySchema: z.ZodType<{
  name: string;
  path: string;
  type: 'file' | 'directory';
  children?: Array<{ name: string; path: string; type: 'file' | 'directory'; children?: unknown[] }>;
}> = z.object({
  name: z.string(),
  path: z.string(),
  type: z.enum(['file', 'directory']),
  children: z.lazy(() => z.array(FileTreeEntrySchema)).optional(),
});

export const ListDirResponseSchema = z.object({
  entries: z.array(FileTreeEntrySchema),
});

/** GET /api/workspace/recent */
export const RecentWorkspacesResponseSchema = z.object({
  workspaces: z.array(z.string()).optional().default([]),
});

/** GET /api/buddy */
export const BuddyGetResponseSchema = z.object({
  state: z.string().optional().default('idle'),
  animation: z.string().optional(),
}).passthrough();
