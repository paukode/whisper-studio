/* Shared constants, types, and pure helpers for the chat composer.
 *
 * Extracted from ChatInput.tsx so the component, the autocomplete hook
 * (useChatAutocomplete), and the slash-command hook (useSlashCommands)
 * all reference one source of truth. Nothing here touches React state —
 * pure data + one pure function (matchSubmitTrigger). */

import type { IconName } from '@/components/chat/activityIcons';

/* Human-readable summary of the file types the attach button, drag-drop, and
 * the @ menu's files accept. Shown in the attach-button tooltip and the @ menu
 * so the supported types are discoverable at the point of use. Mirrors the backend router (server/attachments.py + server/extract). */
export const SUPPORTED_ATTACHMENT_SUMMARY =
  'images, PDF, Office docs, audio, video, code, text';

/* ── Slash command definitions ── */

/* A slash-command argument option. A plain string is both shown and inserted
 * verbatim (e.g. `/effort low`). The object form lets the label shown in the
 * autocomplete differ from the value inserted after the command — used by
 * `/model` so the picker shows the version label ("Sonnet 5.5") while the
 * command still inserts the model key ("sonnet5.5"), matching the model box under
 * the composer. */
export type SlashOption = string | { label: string; value: string };
export const optLabel = (o: SlashOption): string => (typeof o === 'string' ? o : o.label);
export const optValue = (o: SlashOption): string => (typeof o === 'string' ? o : o.value);

export interface SlashCommand {
  cmd: string;
  /** An ICON key (activityIcons.tsx): the menus draw SVG icons, never emoji. */
  icon: IconName;
  desc: string;
  category?: string;
  options?: SlashOption[];
}

// Base list — the `model` entry's `options` is hydrated inside the component
// from useSettingsStore.models so adding a new entry in config.user.json surfaces
// in the slash-command autocomplete without code changes here.
//
// KEEP THIS LIST ALPHABETICAL BY `cmd`. The autocomplete dropdown and the
// /help dialog render in array order, so alphabetical entries make commands
// easy to scan and find. When adding a command, insert it in sorted position.
export const BASE_SLASH_COMMANDS: SlashCommand[] = [
  { cmd: 'ci ', icon: 'checkSquare', desc: 'Watch CI for a branch, or `ci autofix` a failing run', category: 'ai' },
  { cmd: 'clear ', icon: 'x', desc: 'Clear chat display', category: 'general' },
  { cmd: 'compact ', icon: 'layers', desc: 'Summarize the conversation so far (add a note to say what to keep)', category: 'ai' },
  { cmd: 'doctor ', icon: 'pulse', desc: 'Run diagnostics', category: 'debug' },
  { cmd: 'doctor context ', icon: 'ruler', desc: 'Rightsize the system prompt', category: 'debug' },
  { cmd: 'effort ', icon: 'sliders', desc: 'Set thinking depth', options: ['low', 'medium', 'high', 'extra', 'max', 'ultracode'], category: 'ai' },
  { cmd: 'export ', icon: 'arrowDown', desc: 'Export conversation as text', category: 'general' },
  { cmd: 'goal ', icon: 'flag', desc: 'Set a goal and start working toward it (clear to end, gate add <cmd> for a must-pass check)', category: 'ai' },
  { cmd: 'help ', icon: 'info', desc: 'Show available commands', category: 'general' },
  { cmd: 'mcp ', icon: 'link', desc: 'Manage MCP servers in Settings', category: 'settings' },
  { cmd: 'memory ', icon: 'database', desc: 'Toggle memory (on/off/status)', options: ['on', 'off', 'status'], category: 'ai' },
  { cmd: 'model ', icon: 'sparkle', desc: 'Switch chat model', category: 'ai' },
  { cmd: 'notify ', icon: 'bell', desc: 'Toggle browser notifications', category: 'settings' },
  { cmd: 'plan ', icon: 'list', desc: 'Enable plan mode', category: 'ai' },
  { cmd: 'rename ', icon: 'pencil', desc: 'Rename this session', category: 'general' },
  { cmd: 'settings ', icon: 'wrench', desc: 'Open settings', category: 'settings' },
  { cmd: 'skills ', icon: 'zap', desc: 'Manage skills in Settings', category: 'settings' },
  { cmd: 'theme ', icon: 'moon', desc: 'Switch theme', options: ['auto', 'light', 'dark'], category: 'settings' },
  { cmd: 'workflow ', icon: 'graph', desc: 'Run a saved workflow by name', category: 'ai' },
  { cmd: 'workspace ', icon: 'folder', desc: 'Connect workspace', category: 'workspace' },
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

export interface SessionLike {
  id: string;
  title: string;
}

/** Turn a session title into the bare `@<slug>` token typed/inserted for a
 *  mention — lowercase, non-alphanumeric runs collapsed to one underscore,
 *  no leading/trailing underscore. Must match server/agent_tools/
 *  cross_session.py's `_session_context_slug` exactly: the frontend only
 *  needs this to build the insert text, the backend independently slugifies
 *  every session title the same way to resolve it back. */
export function slugifySessionTitle(title: string): string {
  return title.toLowerCase().replace(/[^a-z0-9]+/g, '_').replace(/^_+|_+$/g, '') || 'session';
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
