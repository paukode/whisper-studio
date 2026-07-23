/* Shared constants, types, and pure helpers for the chat composer.
 *
 * Extracted from ChatInput.tsx so the component, the autocomplete hook
 * (useChatAutocomplete), and the slash-command hook (useSlashCommands)
 * all reference one source of truth. Nothing here touches React state —
 * pure data + one pure function (matchSubmitTrigger). */

/* Human-readable summary of the file types the attach button, drag-drop, and
 * the /file: and @file: flows accept. Shown in the attach-button tooltip and
 * the file-search submenu so the supported types are discoverable at the point
 * of use. Mirrors the backend router (server/attachments.py + server/extract). */
export const SUPPORTED_ATTACHMENT_SUMMARY =
  'images, PDF, Office docs, audio, video, code, text';

/* ── Slash command definitions (matching vanilla commands.js) ── */

/* A slash-command argument option. A plain string is both shown and inserted
 * verbatim (e.g. `/effort low`). The object form lets the label shown in the
 * autocomplete differ from the value inserted after the command — used by
 * `/model` so the picker shows the version label ("Sonnet 4.6") while the
 * command still inserts the model key ("sonnet"), matching the model box under
 * the composer. */
export type SlashOption = string | { label: string; value: string };
export const optLabel = (o: SlashOption): string => (typeof o === 'string' ? o : o.label);
export const optValue = (o: SlashOption): string => (typeof o === 'string' ? o : o.value);

export interface SlashCommand {
  cmd: string;
  icon: string;
  desc: string;
  category?: string;
  options?: SlashOption[];
  submenu?: boolean;
}

// Base list — the `model` entry's `options` is hydrated inside the component
// from useSettingsStore.models so adding a new entry in config.json surfaces
// in the slash-command autocomplete without code changes here.
//
// KEEP THIS LIST ALPHABETICAL BY `cmd`. The autocomplete dropdown and the
// /help dialog render in array order, so alphabetical entries make commands
// easy to scan and find. When adding a command, insert it in sorted position.
export const BASE_SLASH_COMMANDS: SlashCommand[] = [
  { cmd: 'btw ', icon: '💬', desc: 'Ask a side question', category: 'ai' },
  { cmd: 'clear ', icon: '🧹', desc: 'Clear chat display', category: 'general' },
  { cmd: 'doctor ', icon: '🩺', desc: 'Run diagnostics', category: 'debug' },
  { cmd: 'effort ', icon: '🎯', desc: 'Set thinking depth', options: ['low', 'medium', 'high', 'extra', 'max', 'ultracode'], category: 'ai' },
  { cmd: 'export ', icon: '💾', desc: 'Export conversation as text', category: 'general' },
  { cmd: 'file:', icon: '📂', desc: 'Reference a workspace file', submenu: true, category: 'workspace' },
  { cmd: 'goal ', icon: '🎯', desc: 'Set a goal the loop works toward (clear to end)', category: 'ai' },
  { cmd: 'help ', icon: '❓', desc: 'Show available commands', category: 'general' },
  { cmd: 'mcp:', icon: '🔌', desc: 'Browse MCP tools', submenu: true, category: 'workspace' },
  { cmd: 'memory ', icon: '💡', desc: 'Toggle memory (on/off/status)', options: ['on', 'off', 'status'], category: 'ai' },
  { cmd: 'model ', icon: '🧠', desc: 'Switch chat model', category: 'ai' },
  { cmd: 'notify ', icon: '🔔', desc: 'Toggle browser notifications', category: 'settings' },
  { cmd: 'plan ', icon: '📋', desc: 'Enable plan mode', category: 'ai' },
  { cmd: 'rename ', icon: '✏️', desc: 'Rename this session', category: 'general' },
  { cmd: 'settings ', icon: '⚙️', desc: 'Open settings', category: 'settings' },
  { cmd: 'skills:', icon: '⚡', desc: 'Browse available skills', submenu: true, category: 'workspace' },
  { cmd: 'subagent ', icon: '🤖', desc: 'Spawn a focused subagent for a one-shot task', category: 'ai' },
  { cmd: 'theme ', icon: '🎨', desc: 'Switch theme', options: ['auto', 'dark', 'light', 'dark-high-contrast', 'light-high-contrast', 'dark-daltonized', 'light-daltonized'], category: 'settings' },
  { cmd: 'verbosity ', icon: '📏', desc: 'Set response length', options: ['brief', 'normal', 'detailed'], category: 'ai' },
  { cmd: 'workflow ', icon: '⚙️', desc: 'Run a saved workflow by name', category: 'ai' },
  { cmd: 'ci ', icon: '🧪', desc: 'Watch CI for a branch, or `ci autofix` a failing run', category: 'ai' },
  { cmd: 'workspace ', icon: '📁', desc: 'Connect workspace', category: 'workspace' },
];

export const HELP_CATEGORY_ORDER = ['general', 'ai', 'workspace', 'settings', 'debug'];
export const HELP_CATEGORY_LABELS: Record<string, string> = {
  general: 'General', ai: 'AI & Model', workspace: 'Workspace',
  settings: 'Settings', debug: 'Debug',
};

/* Canonical example phrases shown to the user in the live dictation hint. The
 * actual matcher (matchSubmitTrigger) is a forgiving grammar, not a literal
 * lookup of this list, so natural variants like "submit it now" or "okay, send"
 * also work. Keep these as clear, representative examples. */
export const VOICE_SUBMIT_TRIGGERS = [
  'okay send', 'okay submit',
  'send now', 'submit now', 'send it now',
  'send away', 'fire away',
  'send the message',
] as const;

/* Forgiving grammar for the hands-free submit command, matched at the very end
 * of the running dictation. A command is an action verb (send/submit/fire) that
 * is either preceded by a lead-in ("okay send") or followed by a closing word
 * ("send now", "submit it now", "send away"), optionally both, optionally
 * trailed by a polite filler ("send now, please"). This is deliberate:
 *   - A bare verb ("send") or bare lead ("okay") never fires, so a one-syllable
 *     mishear cannot submit by accident.
 *   - verb + an ambiguous word ("send it", "submit this") is excluded because
 *     those end real requests.
 *   - Gaps allow whitespace AND punctuation, because speech-to-text inserts a
 *     comma on the pause ("Okay, send"). */
const _GAP = '[\\s.,!?;:]+';
const _LEAD = '(?:okay|ok|alright)';
const _ACTION = '(?:send|submit|fire)';
const _TAIL = '(?:it now|now|away|the message|message)';
const _FILLER = `(?:${_GAP}(?:okay|ok|please|thanks|thank you))*`;
const _CMD = `(?:${_LEAD}${_GAP}${_ACTION}(?:${_GAP}${_TAIL})?|${_ACTION}${_GAP}${_TAIL})`;
const SUBMIT_RE = new RegExp(`(?:^|\\s)(${_CMD})${_FILLER}[\\s.,!?;:]*$`, 'i');

/** Detect a hands-free submit command at the END of the given text.
 *
 *  `fragment` is the whole running dictation, not just the latest piece, so the
 *  command still fires when speech-to-text splits it across fragments or adds a
 *  stray trailing word. Returns the matched command and the message body BEFORE
 *  it (the command words are stripped from what gets sent), or null if the text
 *  does not end with a command. See SUBMIT_RE for the grammar. */
export function matchSubmitTrigger(fragment: string): { hit: string; body: string } | null {
  const m = fragment.match(SUBMIT_RE);
  if (!m) return null;
  const idx = m.index ?? 0;
  const body = fragment.slice(0, idx).replace(/[\s.,;:!?]+$/, '').trim();
  // Normalize the matched command for the return value: collapse the
  // whitespace/punctuation gaps the speech engine inserted into single spaces.
  return { hit: m[1].toLowerCase().replace(/[\s.,!?;:]+/g, ' ').trim(), body };
}

/** Resolve a finalized dictation fragment against the running input.
 *
 *  `base` is the committed input text before this fragment; `clean` is the new
 *  finalized fragment (already trimmed). Returns the next input value and, when
 *  a submit command was spoken at the end, the message to send with the command
 *  stripped (or null when there is nothing to send).
 *
 *  Pure and synchronous on purpose: the caller must NOT decide whether to submit
 *  by reading variables mutated inside a React state updater. Updaters do not run
 *  synchronously under automatic batching (mid-dictation there is always a
 *  pending interim update), so that pattern silently dropped the submit while
 *  still stripping the words. Compute here, then act. */
export function resolveDictationFinal(base: string, clean: string): { text: string; submit: string | null } {
  if (!clean) return { text: base, submit: null };
  const sep = base && !/\s$/.test(base) ? ' ' : '';
  const combined = base + sep + clean;
  const trigger = matchSubmitTrigger(combined);
  if (trigger) {
    const body = trigger.body.trim();
    return { text: '', submit: body || null };
  }
  return { text: combined, submit: null };
}

/* ── @skill mention parsing ── */

/** Synthetic anchor sent as the payload when the user submits a bare `@skill`
 *  mention with no text of their own. Keeps the API message non-empty and
 *  gives the forced tool call something to anchor on. NEVER shown in the UI —
 *  the bubble renders `displayText` (what the user actually typed) instead. */
export const SKILL_MENTION_PLACEHOLDER =
  'Run the requested skill on the conversation context.';

const SKILL_MENTION_RE = /^@(?:skills:)?([a-zA-Z_][\w-]*)\b\s*/;

/** Split an `@skills:<name>` / `@<name>` prefix off a submitted message.
 *
 *  Returns the skill to force server-side (via tool_choice), the payload to
 *  send to the model (placeholder-substituted when the mention was bare), and
 *  the text the chat bubble should display (exactly what the user typed after
 *  the mention — possibly empty). Without a mention, the message passes
 *  through untouched. */
export function parseSkillMention(trimmed: string): {
  forceSkill?: string;
  messageToSend: string;
  displayText?: string;
} {
  const m = trimmed.match(SKILL_MENTION_RE);
  if (!m || !m[1]) return { messageToSend: trimmed };
  const stripped = trimmed.slice(m[0].length).trim();
  return {
    forceSkill: m[1],
    messageToSend: stripped || SKILL_MENTION_PLACEHOLDER,
    displayText: stripped,
  };
}

/** Parse a colon-submenu search prefix (`/file:`, `/skills:`, `/mcp:`) from
 *  the text before the cursor.
 *
 *  Returns `{ type, query }` ONLY while the user is still typing a single-token
 *  argument (one optional leading space allowed, e.g. `/file: report`). As soon
 *  as a further space begins the message body — `/file: report.pdf summarize it`
 *  — this returns `null`, so the search popup dismisses instead of greedily
 *  swallowing the rest of the line. Returns `null` for any non-submenu text. */
export function matchColonSubmenu(before: string): { type: string; query: string } | null {
  const m = before.match(/^\/(file|skills|mcp):\s?(\S*)$/i);
  if (!m) return null;
  return { type: m[1].toLowerCase(), query: m[2] };
}

/* ── Autocomplete item type ── */
export interface ACItem {
  icon: string;
  name: string;
  desc: string;
  insert: string;
}

/* ── @ mention root entries ── */
export const AT_ROOT_ENTRIES: ACItem[] = [
  { icon: '📂', name: '@file:', desc: 'Reference a workspace file', insert: '@file:' },
  { icon: '⚡', name: '@skills:', desc: 'Browse skills', insert: '@skills:' },
  { icon: '🔌', name: '@mcp:', desc: 'Browse MCP tools', insert: '@mcp:' },
];

/* Structural shapes the autocomplete hook reads off the settings store.
 * Declared here (rather than imported) so the hook stays decoupled from
 * the store's internal interfaces — the store's richer types remain
 * assignable to these. */
export interface SkillLike {
  name: string;
  description?: string;
  enabled?: boolean;
}

export interface McpServerLike {
  name: string;
  status: string;
}

export interface ModelOption {
  key: string;
  name: string;
  /** Effort levels this model exposes (empty ⇒ no effort). Used by the
   *  model-aware /effort validator. */
  effort_levels?: string[];
  /** On-device model — runs via the local runtime, not Bedrock. */
  is_local?: boolean;
}
