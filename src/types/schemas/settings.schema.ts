import { z } from 'zod';

/** GET /api/config */
export const AppConfigResponseSchema = z.object({
  // Coerce a blank/whitespace region to the default. An empty string is a
  // valid z.string() and would slip past .default() (which only fills in for
  // undefined), then crash Bedrock client construction with an invalid endpoint.
  bedrock_region: z
    .string()
    .optional()
    .transform((v) => (v && v.trim() ? v.trim() : 'us-east-1')),
  chat_models: z.record(z.string(), z.string()).optional().default({}),
  default_chat_model: z.string().optional().default(''),
  effort_level: z.string().optional().default('high'),
  brief_mode: z.boolean().optional().default(false),
  permission_mode: z.string().optional().default('default'),
  auto_mode: z.boolean().optional().default(false),
  transcription_backend: z.enum(['whisper', 'streaming']).optional().default('streaming'),
  // Where indexing/RAG capabilities run: cloud (Bedrock) | hybrid (per-capability) | local (on-device).
  model_mode: z.enum(['cloud', 'hybrid', 'local']).optional().default('cloud'),
  // Per-capability backend overrides, consulted only in hybrid mode.
  backends: z.record(z.string(), z.string()).optional().default({}),
}).passthrough();

/** GET /api/models */
export const ModelsResponseSchema = z.object({
  models: z.array(z.object({
    key: z.string(),
    name: z.string(),
    // Mythos-class models (Fable 5) require account-wide Bedrock data retention.
    requires_data_retention: z.boolean().optional().default(false),
    // On-device model — runs via the local runtime, not Bedrock.
    is_local: z.boolean().optional().default(false),
    // Whether this local model has a toggleable thinking/reasoning mode.
    supports_thinking: z.boolean().optional().default(false),
    // Whether this local model can use tools (local agentic loop).
    supports_tools: z.boolean().optional().default(false),
    // Per-model effort catalogue (empty ⇒ no effort, e.g. Haiku).
    effort_levels: z.array(z.string()).optional().default([]),
    default_effort: z.string().optional().default('high'),
    supports_ultracode: z.boolean().optional().default(false),
    // GPT-5.x verbosity control (text.verbosity); openai_bedrock models only.
    supports_verbosity: z.boolean().optional().default(false),
    default_verbosity: z.string().optional().default('medium'),
  })).optional().default([]),
  default: z.string().optional().default('opus4.8'),
});

/** GET / PUT /api/data-retention */
export const DataRetentionResponseSchema = z.object({
  mode: z.string().optional().default(''),
  enabled: z.boolean().optional().default(false),
}).passthrough();

/** GET /api/permissions */
export const PermissionsResponseSchema = z.object({
  mode: z.string().optional().default('default'),
}).passthrough();

/** GET /api/mcp/servers */
export const MCPServersResponseSchema = z.object({
  servers: z.record(z.string(), z.object({
    name: z.string().optional(),
    command: z.string(),
    args: z.array(z.string()),
    // Persisted opt-in flag. Older backends may omit it; default to false
    // since that's the new safe default (no tokens spent unless opted in).
    enabled: z.boolean().optional().default(false),
    status: z.string(),
    error: z.string().nullable().optional(),
  })).optional().default({}),
});

/** GET /api/skills */
export const SkillsResponseSchema = z.object({
  skills: z.array(z.object({
    name: z.string(),
    description: z.string().optional(),
    enabled: z.boolean(),
    isFolder: z.boolean().optional(),
    hasScripts: z.boolean().optional(),
    trusted: z.boolean().optional(),
  })).optional().default([]),
  mcpTools: z.array(z.object({
    name: z.string(),
    description: z.string(),
    server: z.string(),
  })).optional().default([]),
});
