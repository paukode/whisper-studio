/** Inline cron card payload. Persisted as a ChatMessage row with
 *  role='cron_event' so it shows up both live (via SSE) and on
 *  session resume. Never enters Claude's prompt — the backend's
 *  visible_chat_history() filter drops cron_event rows before
 *  building the Bedrock messages array. */
export interface CronEventPayload {
  event_type: 'cron_created' | 'cron_fired' | 'cron_deleted' | 'cron_updated';
  cron_id: string;
  cron_name: string;
  run_id?: string;
  status?: 'ok' | 'failed';
  text?: string;
  /** Legacy — kept for old persisted rows. New cards use schedule_label. */
  interval_minutes?: number;
  /** Human-readable schedule, e.g. "every day at 09:00 · Europe/Warsaw". */
  schedule_label?: string;
  /** ISO of the next scheduled fire (absent/irrelevant for deleted). */
  next_run?: string;
  /** How long a fired run took, in ms. */
  duration_ms?: number;
  timestamp: string;
}

/** Inline background-task card payload. Persisted as a ChatMessage row with
 *  role='task_event' (UI-only, same contract as cron_event): announces a
 *  background shell command, detached agent, or workflow run starting or
 *  finishing. Emitted by server/tasks/events.py. */
export interface TaskEventPayload {
  event_type: 'task_started' | 'task_completed' | 'task_failed' | 'task_stopped';
  task_id: string;
  kind: 'shell' | 'agent' | 'workflow';
  title: string;
  status: string;
  exit_code?: number | null;
  duration_ms?: number | null;
  result_tail?: string;
  timestamp: string;
}

export interface ChatMessage {
  role: 'user' | 'assistant' | 'cron_event' | 'task_event';
  content: string;
  timestamp: string;
  /** Populated when role === 'cron_event'. Renders as a CronEventCard
   *  instead of normal message bubble. */
  cronEvent?: CronEventPayload;
  /** Populated when role === 'task_event'. Renders as a BackgroundTaskCard. */
  taskEvent?: TaskEventPayload;
  attachments?: Attachment[];
  attachmentNames?: string[];
  /** Backend attachment ids for this message, so a regenerate/edit-resend
   *  can re-attach the same files instead of dropping them. */
  attachmentIds?: string[];
  thinking?: ThinkingBlock[];
  toolUse?: ToolUseEvent[];
  approvals?: Approval[];
  skills?: string[];
  traces?: SkillTrace[];
  /** Live + final per-team progress, keyed by team_id. Populated by
   *  team_progress SSE events; consumed by TeamReportCard which renders an
   *  expandable per-agent live log alongside the team_create / team_results
   *  tool_use row. */
  teamReports?: Record<string, TeamReportData>;
  _thinkingMs?: number;
  _thinkingText?: string;
  _usage?: { input_tokens: number; output_tokens: number };
  /** Index-grounding summary for this turn: how many indexed folders were
   *  searched and how many passages were injected. `searched: 0` renders as
   *  "no index searched", so a silent no-grounding can't masquerade as the
   *  model failing to find data. Set from the `grounding` SSE event. */
  grounding?: { searched: number; passages: number };
  /** Marker used by useChatStream to know "this message is the question
   *  group for the current streaming round, append to it." Cleared when
   *  the stream finishes (either via [DONE] or finishStream). Never sent
   *  to the backend or persisted. */
  _inFlight?: boolean;
  /** Interactive user question from ask_user tool.
   *
   * Single-question rounds populate `userQuestion`. When the assistant emits
   * MULTIPLE ask_user_question tool calls in one streaming round, we collapse
   * them into `userQuestions` (a list) on a single message. The card renders
   * a single question as clean answer chips (picking one submits immediately)
   * and 2+ questions stacked into one form with a single submit. Both fields
   * can coexist for back-compat — the renderer prefers `userQuestions` when it
   * has at least one entry. `answered` is persisted on submit so a re-render
   * or session restore keeps the card disabled (no duplicate continuation).
   */
  userQuestion?: {
    question: string;
    options: string[];
    toolUseId: string;
    answered?: boolean;
  };
  userQuestions?: Array<{
    question: string;
    options: string[];
    toolUseId: string;
    answered?: boolean;
  }>;
  /** Inline artifact from the create_artifact tool */
  programArtifact?: {
    title: string;
    html: string;
    description: string;
  };
  /** Summary + link card for a plan saved via the create_plan tool. The full
   *  markdown lives in data/plans/ and opens in the dock's plan panel. */
  plan?: {
    id: string;
    title: string;
    summary: string;
  };
}

export interface ThinkingBlock {
  text: string;
  elapsed: number;
}

export interface ToolUseEvent {
  toolId: string;
  toolName: string;
  input: Record<string, unknown>;
  result?: string;
  status: 'pending' | 'running' | 'complete' | 'error';
  /** Populated only for preview_screenshot results — base64 JPEG the model
   *  also received as an image content block, so the human sees exactly
   *  what the model saw. */
  previewImage?: { media_type: string; data: string };
}

export interface SkillTrace {
  name: string;
  input: Record<string, unknown>;
  output: string;
  /** Set once this trace has received its skill_result. Lets repeated
   *  same-named tool calls (e.g. three task_create) each claim their own
   *  result instead of all matching the first trace by name. */
  resolved?: boolean;
  /** See ToolUseEvent.previewImage. */
  previewImage?: { media_type: string; data: string };
}

/** One progress event emitted by an agent runtime turn. Shaped to mirror
 *  the backend payload in server/agents/runtime.py (_emit). */
export interface TeamProgressEvent {
  agent_id?: string;
  agent_name?: string | null;
  agent_type?: string;
  team_id?: string | null;
  /** Set on every event of a spawn_agent child so the card can badge the
   *  row as a spawned child of another agent. */
  parent_agent_id?: string | null;
  phase:
    | 'team_started'
    | 'team_completed'
    | 'started'
    | 'turn_start'
    | 'text'
    | 'tool_call'
    | 'tool_result'
    | 'completed'
    | 'turn_limit'
    | 'failed'
    | 'stopped';
  // payload, phase-dependent
  task?: string;
  model?: string | null;
  max_turns?: number;
  turn?: number;
  turns_used?: number;
  text?: string;
  tool_name?: string;
  tool_input_preview?: string;
  output_preview?: string;
  /** Full (capped ~4KB) tool input/output for the click-to-expand view.
   *  The *_preview fields stay short for the collapsed log line. */
  tool_input_full?: string;
  output_full?: string;
  status?: string;
  error?: string;
  // team_started
  team_name?: string;
  description?: string;
  agents?: Array<{
    name: string;
    task: string;
    agent_type: string;
    role?: 'team' | 'orchestrator';
  }>;
  agents_completed?: number;
}

export interface TeamAgentReport {
  agent_id?: string;
  name: string;
  task: string;
  agent_type: string;
  role: 'team' | 'orchestrator';
  status: 'pending' | 'running' | 'completed' | 'turn_limit' | 'failed' | 'stopped';
  model?: string | null;
  /** Set when this row is a spawn_agent child of another agent. */
  parent_agent_id?: string | null;
  turns_used?: number;
  result?: string;
  events: TeamProgressEvent[];
}

export interface TeamReportData {
  team_id: string;
  team_name: string;
  description?: string;
  status: 'running' | 'completed';
  /** Client-side receipt times (ms epoch) for the elapsed display. */
  started_at?: number;
  completed_at?: number;
  agents: Record<string, TeamAgentReport>; // keyed by agent name (or agent_id if name missing)
  agentOrder: string[];                    // preserves insertion order
}

export interface Approval {
  id: string;
  type: 'file_write' | 'command' | 'file_delete' | 'file_rename';
  path?: string;
  content?: string;
  originalContent?: string;
  command?: string;
  status: 'pending' | 'accepted' | 'denied';
}

/**
 * Generic approval request emitted by the backend. Replaces the per-action
 * WsApproval union — the frontend now reads `preview` to pick a renderer
 * instead of switching on `action`. Categories come from the backend's
 * ApprovalSpec registry and are extensible (no longer a literal union).
 */
export type PreviewKind = 'diff' | 'command' | 'list' | 'text';
export type RiskHint = 'low' | 'medium' | 'high';

export interface ApprovalRequest {
  tool_use_id: string;
  action: string;
  category: string;
  preview: PreviewKind;
  summary: string;
  payload: Record<string, unknown>;
  risk_hint?: RiskHint | null;
  explanation?: string | Record<string, unknown> | null;
}

export interface Attachment {
  id: string;
  name: string;
  type: string;
  size: number;
  url?: string;
}

/** Approval category — extensible string (backend registry decides values). */
export type ApprovalCategory = string;

/** All SSE event types from the server */
export interface SSEEventData {
  // Text streaming
  text?: string;
  thinking?: string;
  thinking_start?: boolean;
  thinking_stop?: boolean;

  // Skill/tool traces
  skill?: string;
  skill_input?: string;
  input?: Record<string, unknown>;
  skill_result?: string;
  output?: string;
  /** Present only alongside a preview_screenshot skill_result. */
  preview_image?: { media_type: string; data: string };

  // Approvals (new generic shape)
  approval_request?: ApprovalRequest;

  // Workspace integration
  ws_auto_applied?: { path?: string; original?: string; content?: string };
  ws_workspace_prompt?: Record<string, unknown>;
  ws_folder_opened?: string;

  // Special cards
  plan_blocked?: Record<string, unknown>;
  security_blocked?: Record<string, unknown>;
  hook_blocked?: { tool_name?: string; reason?: string };
  stop_hook_feedback?: { reason?: string; attempt?: number };
  goal_eval?: { verdict?: string; feedback?: string; confidence?: number; attempt?: number; cap?: number };
  stop_hook_block?: { reason?: string; attempt?: number };
  goal_cap_reached?: { attempt?: number; cap?: number; source?: string };
  workflow_preview?: { script: string; name?: string; description?: string; phases?: unknown[]; budget_usd?: number | null; args?: unknown };
  workflow_started?: { run_id: string; name?: string; resumed_from?: string };
  workflow_event?: Record<string, unknown>;
  ci_started?: { task_id: string; branch?: string };
  ci_diagnosis?: { branch?: string; run_id?: number | null; url?: string | null; findings?: Array<Record<string, unknown>> };
  user_question?: Record<string, unknown>;
  program_artifact?: Record<string, unknown>;
  plan_generated?: Record<string, unknown>;
  notify_user?: Record<string, unknown>;
  /** Progressive tool disclosure telemetry (once per turn). */
  tool_pool?: {
    advertised: number;
    deferred: number;
    total: number;
    deferred_tokens_est: number;
  };
  team_results?: Record<string, unknown>;
  /** Live per-agent progress emitted by server/agents/event_bus.py.
   *  Dispatched by useChatStream into the owning message's `teamReports`
   *  map and rendered by TeamReportCard. */
  team_progress?: TeamProgressEvent;
  /** Inline cron card emitted by server/cron_scheduler.py — both when a
   *  cron is created/deleted (via the chat-driven tool) and when a cron
   *  fires in the background. Dispatched by useChatStream as a fresh
   *  ChatMessage with role='cron_event' so it appears live and is
   *  already in chat_history for replay on session resume. */
  cron_event?: CronEventPayload;

  // Tool result truncation (oversize tool outputs persisted to .whisper_cache/)
  tool_result_truncated?: {
    tool_name?: string;
    full_size?: number;
    kept_bytes?: number;
    cache_filename?: string | null;
    cache_path?: string | null;
  };

  // Usage/tokens
  usage?: {
    input_tokens?: number;
    output_tokens?: number;
    total_input?: number;
    total_output?: number;
    estimated_cost_usd?: number;
    context_used?: number;
    context_max?: number;
  };

  // Session
  resolved_content?: string;
  error?: string;
  /** Emitted once at the head of a turn: how many indexed folders were searched
   *  and how many passages were injected as grounding for this question. */
  grounding?: { searched: number; passages: number };

  // Tasks
  todo_update?: unknown;
}
