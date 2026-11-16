import { z } from 'zod';

/** One row of GET /api/code-tools/status (server/code_tools/status.py). */
export const CodeToolStatusSchema = z.object({
  id: z.string(),
  name: z.string(),
  /** What the tool powers, in plain words. */
  powers: z.string(),
  /** Whether it works here, for the connected workspace. */
  ok: z.boolean(),
  version: z.string().nullable(),
  /** Where it comes from: bundled, the app's environment, the workspace, PATH. */
  source: z.string(),
  /** The exact command the feature runs (empty when there is nothing to run). */
  command: z.string(),
  /** Why it does not work and how to fix it; empty when ok. */
  reason: z.string(),
  /** What else to know when it works (for ruff: whether it fixes and formats). */
  note: z.string(),
});

/** GET /api/code-tools/status */
export const CodeToolsStatusResponseSchema = z.object({
  workspace: z.string().nullable(),
  tools: z.array(CodeToolStatusSchema),
});

/** One file's result from POST /api/code-tools/check. */
export const CodeCheckResultSchema = z.object({
  path: z.string(),
  tool: z.string(),
  /** Number of findings; null when the file could not be checked. */
  issues: z.number().nullable(),
  text: z.string(),
});

/** POST /api/code-tools/check (server/code_tools/turn_checks.py). */
export const CodeChecksResponseSchema = z.object({
  results: z.array(CodeCheckResultSchema),
  issues: z.number(),
  /** The message that carries the findings to the assistant; empty when clean. */
  report: z.string(),
  enabled: z.boolean(),
});

export type CodeChecksResponse = z.infer<typeof CodeChecksResponseSchema>;
export type CodeToolStatus = z.infer<typeof CodeToolStatusSchema>;
export type CodeToolsStatusResponse = z.infer<typeof CodeToolsStatusResponseSchema>;
