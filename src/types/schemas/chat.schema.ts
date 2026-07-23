import { z } from 'zod';

/** Schema for SSE event data parsed from JSON.parse in useChatStream. */
export const SSEEventDataSchema = z.object({
  // Text streaming
  text: z.string().optional(),
  thinking: z.string().optional(),
  thinking_start: z.boolean().optional(),
  thinking_stop: z.boolean().optional(),

  // Skill/tool traces. `skill_input` carries the tool name as a string
  // (truthy-checked by useChatStream); the older boolean form was wrong.
  skill: z.string().optional(),
  skill_input: z.string().optional(),
  input: z.record(z.string(), z.unknown()).optional(),
  skill_result: z.string().optional(),
  output: z.string().optional(),

  // Approvals — new generic shape (replaces legacy ws_approval per-action union)
  approval_request: z.object({
    tool_use_id: z.string(),
    action: z.string(),
    category: z.string(),
    preview: z.enum(['diff', 'command', 'list', 'text']),
    summary: z.string(),
    payload: z.record(z.string(), z.unknown()),
    risk_hint: z.enum(['low', 'medium', 'high']).nullable().optional(),
    explanation: z.union([z.string(), z.record(z.string(), z.unknown())]).nullable().optional(),
  }).optional(),

  // Workspace integration
  ws_auto_applied: z.object({
    path: z.string().optional(),
    original: z.string().optional(),
    content: z.string().optional(),
  }).optional(),
  ws_workspace_prompt: z.record(z.string(), z.unknown()).optional(),
  ws_folder_opened: z.string().optional(),

  // Special cards
  plan_blocked: z.record(z.string(), z.unknown()).optional(),
  security_blocked: z.record(z.string(), z.unknown()).optional(),
  // A blocking hook denied a tool call (non-security shell/project hook).
  hook_blocked: z.object({
    tool_name: z.string().optional(),
    reason: z.string().optional(),
  }).optional(),
  // A Stop hook refused to end the turn; the loop continues.
  stop_hook_feedback: z.object({
    reason: z.string().optional(),
    attempt: z.number().optional(),
  }).optional(),
  // Completion-gate frames (WS-E goal loop).
  goal_eval: z.object({
    verdict: z.string().optional(),
    feedback: z.string().optional(),
    confidence: z.number().optional(),
    attempt: z.number().optional(),
    cap: z.number().optional(),
  }).optional(),
  stop_hook_block: z.object({
    reason: z.string().optional(),
    attempt: z.number().optional(),
  }).optional(),
  goal_cap_reached: z.object({
    attempt: z.number().optional(),
    cap: z.number().optional(),
    source: z.string().optional(),
  }).optional(),
  // Workflow runtime (WS-D).
  workflow_preview: z.object({
    script: z.string(),
    name: z.string().optional(),
    description: z.string().optional(),
    phases: z.array(z.unknown()).optional(),
    budget_usd: z.number().nullable().optional(),
    args: z.unknown().optional(),
  }).optional(),
  workflow_started: z.object({
    run_id: z.string(),
    name: z.string().optional(),
    resumed_from: z.string().optional(),
  }).optional(),
  workflow_event: z.record(z.string(), z.unknown()).optional(),
  user_question: z.record(z.string(), z.unknown()).optional(),
  program_artifact: z.object({
    title: z.string(),
    html: z.string(),
    description: z.string().optional().default(''),
    tool_use_id: z.string().optional(),
  }).optional(),
  notify_user: z.record(z.string(), z.unknown()).optional(),
  tool_pool: z
    .object({
      advertised: z.number(),
      deferred: z.number(),
      total: z.number(),
      deferred_tokens_est: z.number(),
    })
    .optional(),
  team_results: z.record(z.string(), z.unknown()).optional(),
  team_progress: z.object({
    agent_id: z.string().optional(),
    agent_name: z.string().nullable().optional(),
    agent_type: z.string().optional(),
    team_id: z.string().nullable().optional(),
    parent_agent_id: z.string().nullable().optional(),
    phase: z.enum([
      'team_started', 'team_completed', 'started', 'turn_start', 'text',
      'tool_call', 'tool_result', 'completed', 'turn_limit', 'failed',
      'stopped',
    ]),
    task: z.string().optional(),
    model: z.string().nullable().optional(),
    max_turns: z.number().optional(),
    turn: z.number().optional(),
    turns_used: z.number().optional(),
    text: z.string().optional(),
    tool_name: z.string().optional(),
    tool_input_preview: z.string().optional(),
    output_preview: z.string().optional(),
    tool_input_full: z.string().optional(),
    output_full: z.string().optional(),
    status: z.string().optional(),
    error: z.string().optional(),
    team_name: z.string().optional(),
    description: z.string().optional(),
    agents: z.array(z.object({
      name: z.string(),
      task: z.string(),
      agent_type: z.string(),
      role: z.enum(['team', 'orchestrator']).optional(),
    })).optional(),
    agents_completed: z.number().optional(),
  }).passthrough().optional(),
  /** Cron card emitted by server/cron_scheduler.py when a cron is
   *  created/deleted (via tool) or when it fires in the background.
   *  Dispatched as a fresh ChatMessage with role='cron_event'. */
  cron_event: z.object({
    event_type: z.enum(['cron_created', 'cron_fired', 'cron_deleted', 'cron_updated']),
    cron_id: z.string(),
    cron_name: z.string(),
    run_id: z.string().optional(),
    status: z.enum(['ok', 'failed']).optional(),
    text: z.string().optional(),
    interval_minutes: z.number().optional(),
    schedule_label: z.string().optional(),
    next_run: z.string().optional(),
    duration_ms: z.number().optional(),
    timestamp: z.string(),
  }).passthrough().optional(),

  // Tool result truncation
  tool_result_truncated: z.object({
    tool_name: z.string().optional(),
    full_size: z.number().optional(),
    kept_bytes: z.number().optional(),
    cache_filename: z.string().nullable().optional(),
    cache_path: z.string().nullable().optional(),
  }).optional(),

  // Usage/tokens
  usage: z.object({
    input_tokens: z.number().optional(),
    output_tokens: z.number().optional(),
    total_input: z.number().optional(),
    total_output: z.number().optional(),
    estimated_cost_usd: z.number().optional(),
    context_used: z.number().optional(),
    context_max: z.number().optional(),
  }).optional(),

  // Session
  resolved_content: z.string().optional(),
  error: z.string().optional(),
  grounding: z.object({
    searched: z.number(),
    passages: z.number(),
  }).optional(),

  // Tasks
  todo_update: z.unknown().optional(),
}).passthrough();

export type SSEEventDataParsed = z.infer<typeof SSEEventDataSchema>;
