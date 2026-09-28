import { z } from 'zod';

/** GET /api/config */
export const AppConfigResponseSchema = z.object({
  chat_models: z.record(z.string(), z.string()).optional().default({}),
  effort_level: z.string().optional().default('high'),
  brief_mode: z.boolean().optional().default(false),
  permission_mode: z.string().optional().default('default'),
  transcription_backend: z.enum(['whisper', 'streaming']).optional().default('streaming'),
  // Where indexing/RAG capabilities run: cloud (Bedrock) | hybrid (per-capability) | local (on-device).
  model_mode: z.enum(['cloud', 'hybrid', 'local']).optional().default('cloud'),
  // Per-capability backend overrides, consulted only in hybrid mode.
  backends: z.record(z.string(), z.string()).optional().default({}),
  // First-run model-mode notice dismissed (config-backed; see AppConfig).
  mode_notice_seen: z.boolean().optional().default(false),
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
  // '' when the active mode can run nothing (never a hardcoded cloud stand-in).
  default: z.string().optional().default(''),
  // Local mode with no on-device chat model installed.
  needs_local_model: z.boolean().optional().default(false),
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

/** GET /api/mcp/servers: the one MCP server list (src/stores/mcpStore.ts).
 *  The backend coerces every field to these types, so the shape is exact. */
export const MCPServersResponseSchema = z.object({
  servers: z.record(z.string(), z.object({
    command: z.string(),
    args: z.array(z.string()),
    env: z.record(z.string(), z.string()),
    enabled: z.boolean(),
    status: z.string(),
    tools: z.array(z.string()),
    error: z.string().nullable(),
    url: z.string(),
    bearer_token_env_var: z.string(),
    approval_mode: z.string(),
    tool_overrides: z.record(z.string(), z.string()),
    enabled_tools: z.array(z.string()),
    disabled_tools: z.array(z.string()),
  })),
  revision: z.number(),
  config_error: z.string().nullable(),
  pending_elicitations: z.array(z.object({
    elicitation_id: z.string(),
    server: z.string(),
    session_id: z.string().nullable().optional(),
    mode: z.string(),
    message: z.string(),
    requested_schema: z.record(z.string(), z.unknown()).nullable().optional(),
    url: z.string().nullable().optional(),
  })),
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
});
