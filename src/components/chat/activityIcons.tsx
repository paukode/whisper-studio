import React from 'react';

/**
 * The stroke icons of the chat's activity chrome: the glyph each tool is
 * drawn with in an activity row, the delivery chips and the agent card, plus
 * the state marks (chevrons, check, alert, stop) and the thinking block's
 * marks. Every icon is a 12px feather-style SVG drawn in currentColor, so
 * the surrounding slot sets its size and colour; no emoji and no text glyph
 * is used anywhere in chat chrome.
 */
const icon = (children: React.ReactNode, mark?: string) => (
  <svg className={mark ? `activity-ic activity-mark-${mark}` : 'activity-ic'} width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
    {children}
  </svg>
);

export const ICON = {
  file: icon(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /></>),
  filePlus: icon(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="12" y1="12" x2="12" y2="18" /><line x1="9" y1="15" x2="15" y2="15" /></>),
  pencil: icon(<path d="M17 3a2.8 2.8 0 1 1 4 4L7.5 20.5 2 22l1.5-5.5z" />),
  plus: icon(<><line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" /></>),
  x: icon(<><line x1="18" y1="6" x2="6" y2="18" /><line x1="6" y1="6" x2="18" y2="18" /></>),
  search: icon(<><circle cx="11" cy="11" r="7" /><line x1="21" y1="21" x2="16.5" y2="16.5" /></>),
  layers: icon(<><polygon points="12 2 2 7 12 12 22 7 12 2" /><polyline points="2 17 12 22 22 17" /><polyline points="2 12 12 17 22 12" /></>),
  folder: icon(<path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z" />),
  terminal: icon(<><polyline points="4 17 10 11 4 5" /><line x1="12" y1="19" x2="20" y2="19" /></>),
  code: icon(<><polyline points="16 18 22 12 16 6" /><polyline points="8 6 2 12 8 18" /></>),
  checkSquare: icon(<><polyline points="9 11 12 14 22 4" /><path d="M21 12v7a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h11" /></>),
  refresh: icon(<><path d="M21 12a9 9 0 1 1-2.6-6.4" /><path d="M21 3v6h-6" /></>),
  list: icon(<><line x1="8" y1="6" x2="21" y2="6" /><line x1="8" y1="12" x2="21" y2="12" /><line x1="8" y1="18" x2="21" y2="18" /><line x1="3" y1="6" x2="3.01" y2="6" /><line x1="3" y1="12" x2="3.01" y2="12" /><line x1="3" y1="18" x2="3.01" y2="18" /></>),
  globe: icon(<><circle cx="12" cy="12" r="9" /><path d="M3 12h18M12 3c2.5 2.6 3.9 5.7 3.9 9s-1.4 6.4-3.9 9c-2.5-2.6-3.9-5.7-3.9-9S9.5 5.6 12 3z" /></>),
  clock: icon(<><circle cx="12" cy="12" r="9" /><path d="M12 7v5l3.5 2" /></>),
  wrench: icon(<path d="M14.7 6.3a1 1 0 0 0 0 1.4l1.6 1.6a1 1 0 0 0 1.4 0l3.77-3.77a6 6 0 0 1-7.94 7.94l-6.91 6.91a2.12 2.12 0 0 1-3-3l6.91-6.91a6 6 0 0 1 7.94-7.94l-3.76 3.76z" />),
  gitBranch: icon(<><line x1="6" y1="3" x2="6" y2="15" /><circle cx="18" cy="6" r="3" /><circle cx="6" cy="18" r="3" /><path d="M18 9a9 9 0 0 1-9 9" /></>),
  gitCommit: icon(<><circle cx="12" cy="12" r="4" /><line x1="2" y1="12" x2="8" y2="12" /><line x1="16" y1="12" x2="22" y2="12" /></>),
  arrowUp: icon(<><line x1="12" y1="19" x2="12" y2="5" /><polyline points="5 12 12 5 19 12" /></>),
  arrowDown: icon(<><line x1="12" y1="5" x2="12" y2="19" /><polyline points="19 12 12 19 5 12" /></>),
  diff: icon(<><path d="M12 3v14" /><path d="M5 10h14" /><path d="M5 21h14" /></>),
  history: icon(<><path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" /><path d="M12 7v5l3.5 2" /></>),
  box: icon(<><rect x="3" y="4" width="18" height="5" rx="1" /><path d="M5 9v10a1 1 0 0 0 1 1h12a1 1 0 0 0 1-1V9" /><line x1="10" y1="13" x2="14" y2="13" /></>),
  database: icon(<><ellipse cx="12" cy="5" rx="8" ry="3" /><path d="M4 5v6c0 1.7 3.6 3 8 3s8-1.3 8-3V5" /><path d="M4 11v6c0 1.7 3.6 3 8 3s8-1.3 8-3v-6" /></>),
  monitor: icon(<><rect x="2" y="3" width="20" height="14" rx="2" /><line x1="8" y1="21" x2="16" y2="21" /><line x1="12" y1="17" x2="12" y2="21" /></>),
  camera: icon(<><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h3l2-3h6l2 3h3a2 2 0 0 1 2 2z" /><circle cx="12" cy="13" r="4" /></>),
  pointer: icon(<path d="M4 3l7 18 2.4-7.6L21 11z" />),
  pulse: icon(<polyline points="22 12 18 12 15 21 9 3 6 12 2 12" />),
  message: icon(<path d="M21 11.5a8.4 8.4 0 0 1-9 8.4 8.9 8.9 0 0 1-4-.9L3 21l1.9-5a8.4 8.4 0 0 1-.9-3.8 8.4 8.4 0 0 1 8.4-8.4h.5a8.4 8.4 0 0 1 8.1 8.1z" />),
  users: icon(<><path d="M17 21v-2a4 4 0 0 0-4-4H5a4 4 0 0 0-4 4v2" /><circle cx="9" cy="7" r="4" /><path d="M23 21v-2a4 4 0 0 0-3-3.9" /><path d="M16 3.1a4 4 0 0 1 0 7.8" /></>),
  chart: icon(<><line x1="18" y1="20" x2="18" y2="10" /><line x1="12" y1="20" x2="12" y2="4" /><line x1="6" y1="20" x2="6" y2="14" /></>),
  fileText: icon(<><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z" /><polyline points="14 2 14 8 20 8" /><line x1="8" y1="14" x2="15" y2="14" /><line x1="8" y1="18" x2="13" y2="18" /></>),
  table: icon(<><rect x="3" y="3" width="18" height="18" rx="2" /><line x1="3" y1="9" x2="21" y2="9" /><line x1="3" y1="15" x2="21" y2="15" /><line x1="9" y1="3" x2="9" y2="21" /></>),
  slides: icon(<><path d="M2 3h20" /><path d="M4 3v10a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V3" /><path d="M12 15v6" /><path d="M9 21h6" /></>),
  sparkle: icon(<><path d="M12 3l1.9 5.1L19 10l-5.1 1.9L12 17l-1.9-5.1L5 10l5.1-1.9z" /><path d="M19 15l.7 2L22 17.7l-2.3.8L19 21l-.7-2.5L16 17.7l2.3-.7z" /></>),
  cloud: icon(<path d="M17.5 19a4.5 4.5 0 0 0 .5-9 6 6 0 0 0-11.6-1.6A4 4 0 0 0 6.5 19z" />),
  sliders: icon(<><line x1="4" y1="21" x2="4" y2="14" /><line x1="4" y1="10" x2="4" y2="3" /><line x1="12" y1="21" x2="12" y2="12" /><line x1="12" y1="8" x2="12" y2="3" /><line x1="20" y1="21" x2="20" y2="16" /><line x1="20" y1="12" x2="20" y2="3" /><line x1="1" y1="14" x2="7" y2="14" /><line x1="9" y1="8" x2="15" y2="8" /><line x1="17" y1="16" x2="23" y2="16" /></>),
  play: icon(<polygon points="6 4 20 12 6 20 6 4" />),
  book: icon(<><path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" /></>),
  shield: icon(<><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z" /><polyline points="9 12 11 14 15 10" /></>),
  link: icon(<><path d="M10 13a5 5 0 0 0 7.5.5l3-3a5 5 0 0 0-7-7l-1.7 1.7" /><path d="M14 11a5 5 0 0 0-7.5-.5l-3 3a5 5 0 0 0 7 7l1.7-1.7" /></>),
  alert: icon(<><path d="M10.3 3.9 1.8 18a2 2 0 0 0 1.7 3h17a2 2 0 0 0 1.7-3L13.7 3.9a2 2 0 0 0-3.4 0z" /><line x1="12" y1="9" x2="12" y2="13" /><line x1="12" y1="17" x2="12.01" y2="17" /></>),
  info: icon(<><circle cx="12" cy="12" r="9" /><line x1="12" y1="11" x2="12" y2="16" /><line x1="12" y1="8" x2="12.01" y2="8" /></>),
  moon: icon(<path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z" />),
  flag: icon(<><path d="M4 15s1-1 4-1 5 2 8 2 4-1 4-1V3s-1 1-4 1-5-2-8-2-4 1-4 1z" /><line x1="4" y1="22" x2="4" y2="15" /></>),
  mic: icon(<><path d="M12 2a3 3 0 0 0-3 3v7a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" /><path d="M19 10v2a7 7 0 0 1-14 0v-2" /><line x1="12" y1="19" x2="12" y2="22" /></>),
  graph: icon(<><circle cx="18" cy="5" r="3" /><circle cx="6" cy="12" r="3" /><circle cx="18" cy="19" r="3" /><line x1="8.6" y1="10.7" x2="15.4" y2="6.3" /><line x1="8.6" y1="13.3" x2="15.4" y2="17.7" /></>),
  // State marks and chrome. A state mark carries its own class
  // (activity-mark-error, -stopped, -done) so a test or a stylesheet can
  // tell the state from the drawing.
  chevronRight: icon(<polyline points="9 6 15 12 9 18" />),
  chevronDown: icon(<polyline points="6 9 12 15 18 9" />),
  check: icon(<polyline points="20 6 9 17 4 12" />, 'done'),
  alertCircle: icon(<><circle cx="12" cy="12" r="10" /><line x1="12" y1="8" x2="12" y2="12" /><line x1="12" y1="16" x2="12.01" y2="16" /></>, 'error'),
  stop: icon(<rect x="5" y="5" width="14" height="14" rx="2" />, 'stopped'),
  // The thinking block: a thought cloud, and the sparkle for a local model
  // that is generating rather than thinking.
  thought: icon(<><path d="M17.5 16a4.5 4.5 0 0 0 .5-9 6 6 0 0 0-11.6-1.6A4 4 0 0 0 6.5 16z" /><path d="M8 19.5h.01" /><path d="M5 22h.01" /></>),
};

/** Every tool this app can emit gets a glyph. A name that is missing here
 *  falls through to `iconFor`'s wrench, which reads as "some unlabelled
 *  step", so the map tracks the real catalog
 *  (`server/chat/tool_pool.py::assemble_full_catalog`), not the tool-name
 *  vocabulary of any other product. CamelCase keys are kept alongside the
 *  app's own names because recorded transcripts from other vocabularies
 *  still render through the activity row. */
export const TOOL_ICON: Record<string, React.ReactNode> = {
  // Workspace files
  ws_read_file: ICON.file,
  Read: ICON.file,
  ws_write_file: ICON.pencil,
  Write: ICON.pencil,
  ws_edit_file: ICON.pencil,
  Edit: ICON.pencil,
  ws_create_file: ICON.filePlus,
  ws_delete_file: ICON.x,
  ws_open_folder: ICON.folder,
  ws_list_directory: ICON.folder,
  LS: ICON.folder,
  save_file: ICON.fileText,
  notebook_read: ICON.fileText,
  notebook_edit: ICON.pencil,

  // Search and code intelligence
  ws_grep: ICON.search,
  Grep: ICON.search,
  ws_glob: ICON.layers,
  Glob: ICON.layers,
  workspace_semantic_search: ICON.search,
  workspace_graph_query: ICON.graph,
  tool_search: ICON.search,
  lsp_diagnostics: ICON.alert,
  lsp_hover: ICON.info,
  lsp_references: ICON.link,

  // Shell and code execution
  ws_run_command: ICON.terminal,
  Bash: ICON.terminal,
  terminal_run: ICON.terminal,
  terminal_send: ICON.terminal,
  terminal_close: ICON.x,
  run_python: ICON.code,
  office_script: ICON.code,
  run_tool_script: ICON.play,

  // Tasks
  task_create: ICON.checkSquare,
  task_update: ICON.refresh,
  task_list: ICON.list,
  task_get: ICON.list,
  task_status: ICON.pulse,
  task_output: ICON.fileText,
  task_stop: ICON.x,
  task_cancel: ICON.x,
  TodoWrite: ICON.pencil,

  // Agents, teams, messaging
  spawn_agent: ICON.users,
  list_agents: ICON.users,
  team_create: ICON.users,
  team_delete: ICON.x,
  team_results: ICON.fileText,
  promote_agent_type: ICON.users,
  complete_coordination: ICON.checkSquare,
  send_message: ICON.message,
  send_session_message: ICON.message,
  receive_messages: ICON.message,
  notify_user: ICON.message,
  ask_user_question: ICON.message,

  // Git
  git_status: ICON.list,
  git_diff: ICON.diff,
  git_show: ICON.diff,
  git_blame: ICON.history,
  git_log: ICON.history,
  git_add_commit: ICON.gitCommit,
  git_branch_list: ICON.gitBranch,
  git_create_branch: ICON.gitBranch,
  git_checkout: ICON.gitBranch,
  git_merge: ICON.gitBranch,
  git_delete_branch: ICON.x,
  git_push: ICON.arrowUp,
  git_push_pr: ICON.arrowUp,
  git_pull: ICON.arrowDown,
  git_fetch: ICON.arrowDown,
  git_clone: ICON.arrowDown,
  git_stash: ICON.box,
  git_stash_list: ICON.box,
  ws_create_worktree: ICON.gitBranch,
  ws_merge_worktree: ICON.gitBranch,
  ws_diff_worktree: ICON.diff,

  // GitHub and CI
  github: ICON.globe,
  github_api: ICON.globe,
  github_api_write: ICON.globe,
  github_actions: ICON.pulse,
  ci_status: ICON.pulse,
  ci_watch: ICON.pulse,
  ci_autofix: ICON.wrench,
  verify_change: ICON.shield,

  // Live preview
  preview_start: ICON.monitor,
  preview_stop: ICON.x,
  preview_list: ICON.monitor,
  preview_resize: ICON.monitor,
  preview_snapshot: ICON.monitor,
  preview_navigate: ICON.globe,
  preview_click: ICON.pointer,
  preview_fill: ICON.pointer,
  preview_inspect: ICON.search,
  preview_screenshot: ICON.camera,
  preview_eval: ICON.code,
  preview_logs: ICON.pulse,
  preview_console_logs: ICON.pulse,
  preview_network: ICON.pulse,

  // Memory
  memory_read: ICON.database,
  memory_write: ICON.database,
  memory_list: ICON.database,
  memory_delete: ICON.x,

  // Documents, artifacts, deliverables
  create_docx: ICON.fileText,
  create_pdf: ICON.fileText,
  create_pptx: ICON.slides,
  create_xlsx: ICON.table,
  create_chart: ICON.chart,
  create_visual: ICON.chart,
  create_artifact: ICON.sparkle,
  edit_artifact: ICON.sparkle,
  read_artifact: ICON.sparkle,
  create_program: ICON.sparkle,
  create_plan: ICON.flag,
  analyze_document: ICON.fileText,
  inspect_document: ICON.search,
  summarize_transcript: ICON.mic,

  // Skills, workflows, schedules
  skill_invoke: ICON.play,
  skill_list: ICON.book,
  skill_manage: ICON.book,
  workflow_run: ICON.play,
  workflow_list: ICON.list,
  workflow_save: ICON.graph,
  workflow_status: ICON.pulse,
  workflow_delete: ICON.x,
  cron_create: ICON.clock,
  cron_update: ICON.clock,
  cron_delete: ICON.clock,
  cron_list: ICON.clock,
  cron_run: ICON.play,

  // Sessions, config, services
  list_sessions: ICON.history,
  session_search: ICON.history,
  read_cached_result: ICON.box,
  config_get: ICON.sliders,
  config_set: ICON.sliders,
  list_mcp_resources: ICON.link,
  read_mcp_resource: ICON.link,
  aws_boto3: ICON.cloud,
  aws_cli: ICON.cloud,
  sleep: ICON.moon,

  // Web. Two vocabularies land here: this app's catalog emits web_fetch /
  // web_search (server/executors/tools.py), while transcripts recorded
  // under other tool-name schemes carry the CamelCase spellings.
  web_fetch: ICON.globe,
  WebFetch: ICON.globe,
  web_search: ICON.search,
  WebSearch: ICON.search,

  // The synthetic cards the server raises (sseEventCards.ts).
  goal_eval: ICON.flag,
  goal_cap_reached: ICON.flag,
  stop_hook_block: ICON.flag,
  stop_hook_feedback: ICON.flag,
  hook_blocked: ICON.shield,
  security_blocked: ICON.shield,
  plan_blocked: ICON.shield,
};

export function iconFor(name: string): React.ReactNode {
  return TOOL_ICON[name] ?? ICON.wrench;
}
