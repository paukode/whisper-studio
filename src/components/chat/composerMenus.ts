/* The composer's two menus, as pure data: `/` runs something, `@` points at
 * something to use as context. useChatAutocomplete shows them, and ChatInput
 * sends what they insert.
 *
 * `/` lists the built-in commands, the enabled skills (`/skill-name`) and the
 * prompts of connected MCP servers (`/server:prompt`). `@` lists workspace
 * files and folders (attached as chips, never inserted as text), agent types
 * (`@agent-<type>`), other sessions (`@<session>`), MCP resources
 * (`@server:uri`), `@index` and `@docs`. The server resolves the @ text
 * (server/chat/mentions.py). */

import type { IconName } from '@/components/chat/activityIcons';
import {
  slugifySessionTitle,
  SUPPORTED_ATTACHMENT_SUMMARY,
  type SessionLike,
  type SlashCommand,
} from './chatInputConstants';

export type ACKind =
  | 'command'
  | 'option'
  | 'skill'
  | 'prompt'
  | 'file'
  | 'folder'
  | 'agent'
  | 'session'
  | 'resource'
  | 'source'
  | 'hint';

/* ── Autocomplete item type ── */
export interface ACItem {
  icon: IconName;
  name: string;
  desc: string;
  /** The text the item puts in the composer (empty for files, folders and
   *  hints, which insert nothing). */
  insert: string;
  kind: ACKind;
  /** A workspace file or folder: its path. */
  path?: string;
}

/* Structural shapes read off the stores, declared here so the menus stay
 * decoupled from the stores' own interfaces. */
export interface SkillLike {
  name: string;
  description?: string;
  enabled?: boolean;
}

export interface McpPromptLike {
  command: string;
  description: string;
  arguments: Array<{ name: string; required: boolean }>;
}

export interface McpResourceLike {
  mention: string;
  name: string;
  description: string;
}

export interface AgentTypeLike {
  name: string;
  description: string;
}

/** Synthetic anchor sent as the payload when the user runs a bare `/skill`
 *  with no text of their own. Keeps the API message non-empty and gives the
 *  model's call of the requested skill something to anchor on. NEVER shown in
 *  the UI: the bubble renders `displayText` (what the user typed) instead. */
export const SKILL_COMMAND_PLACEHOLDER = 'Run the requested skill on the conversation context.';

/** The arguments of an MCP prompt, as its row in the `/` menu shows them:
 *  optional ones in brackets. */
function promptDesc(p: McpPromptLike): string {
  const args = p.arguments.map((a) => (a.required ? a.name : `[${a.name}]`)).join(' ');
  const what = p.description || 'MCP prompt';
  return args ? `${what} (${args})` : what;
}

const includes = (hay: string, q: string) => hay.toLowerCase().includes(q);

/** The `/` menu for the text typed after the slash. */
export function slashItems(
  query: string,
  ctx: { commands: SlashCommand[]; skills: SkillLike[]; prompts: McpPromptLike[] },
): ACItem[] {
  const q = query.toLowerCase();
  const builtIn = new Set(ctx.commands.map((c) => c.cmd.trim().toLowerCase()));
  const commands: ACItem[] = ctx.commands
    .filter((c) => c.cmd.toLowerCase().startsWith(q))
    .map((c) => ({ icon: c.icon, name: '/' + c.cmd.trim(), desc: c.desc, insert: c.cmd, kind: 'command' }));
  // A skill named like a built-in command cannot run from `/`: the command wins.
  const skills: ACItem[] = ctx.skills
    .filter((s) => s.enabled !== false && !builtIn.has(s.name.toLowerCase()) && includes(s.name, q))
    .map((s) => ({ icon: 'zap', name: '/' + s.name, desc: s.description || 'Skill', insert: s.name + ' ', kind: 'skill' }));
  const prompts: ACItem[] = ctx.prompts
    .filter((p) => includes(p.command, q))
    .map((p) => ({ icon: 'link', name: '/' + p.command, desc: promptDesc(p), insert: p.command + ' ', kind: 'prompt' }));
  return [...commands, ...skills, ...prompts];
}

const SOURCES: ACItem[] = [
  { icon: 'search', name: '@index', desc: 'Search your indexed folders for this message', insert: '@index', kind: 'source' },
  { icon: 'book', name: '@docs', desc: 'Ask the app documentation (answers cite pages)', insert: '@docs', kind: 'source' },
];

/** With nothing typed after the `@`, the menu shows this many sessions and
 *  resources (typing narrows them to every match). */
const UNFILTERED_SESSIONS = 10;
const UNFILTERED_RESOURCES = 10;

/** The `@` menu for the text typed after the `@`, without the workspace
 *  files and folders (they come from a search, see pathItems). */
export function atItems(
  query: string,
  ctx: { sessions: SessionLike[]; agents: AgentTypeLike[]; resources: McpResourceLike[]; workspace: boolean },
): ACItem[] {
  const q = query.toLowerCase();
  const agents: ACItem[] = ctx.agents
    .filter((a) => includes('agent-' + a.name, q))
    .map((a) => ({ icon: 'users', name: '@agent-' + a.name, desc: a.description || 'Agent', insert: '@agent-' + a.name, kind: 'agent' }));
  const sessions: ACItem[] = ctx.sessions
    .filter((s) => !q || includes(s.title, q) || slugifySessionTitle(s.title).includes(q))
    .slice(0, q ? undefined : UNFILTERED_SESSIONS)
    .map((s) => ({
      icon: 'message',
      name: '@' + s.title,
      desc: "Pull this session's recent conversation in as context",
      insert: '@' + slugifySessionTitle(s.title),
      kind: 'session',
    }));
  const resources: ACItem[] = ctx.resources
    .filter((r) => !q || includes(r.mention, q) || includes(r.name, q))
    .slice(0, q ? undefined : UNFILTERED_RESOURCES)
    .map((r) => ({ icon: 'link', name: '@' + r.mention, desc: r.description || r.name, insert: '@' + r.mention, kind: 'resource' }));
  const sources = SOURCES.filter((s) => includes(s.name, q));
  const hint: ACItem[] = ctx.workspace && !q
    ? [{ icon: 'folder', name: 'Type to search files and folders', desc: `Attach ${SUPPORTED_ATTACHMENT_SUMMARY}`, insert: '', kind: 'hint' }]
    : [];
  return [...hint, ...agents, ...sessions, ...resources, ...sources];
}

/** Workspace search results as `@` menu rows: a pick attaches the path as a
 *  chip, so nothing is inserted. */
export function pathItems(results: Array<{ path: string; folder?: boolean }>): ACItem[] {
  return results.map((r) => (r.folder
    ? { icon: 'folder', name: r.path + '/', desc: 'Workspace folder: attach its listing', insert: '', kind: 'folder', path: r.path }
    : { icon: 'file', name: r.path, desc: 'Workspace file', insert: '', kind: 'file', path: r.path }));
}

/** What a sent `/...` message runs when it is no built-in command: a skill
 *  (`/skill-name text`) or an MCP prompt (`/server:prompt args`). Null when it
 *  names neither, and the text goes to the model as typed. */
export type SlashSend =
  | { kind: 'skill'; name: string; rest: string }
  | { kind: 'prompt'; command: string; args: string };

export function parseSlashSend(
  text: string,
  ctx: { skills: SkillLike[]; prompts: McpPromptLike[] },
): SlashSend | null {
  const m = /^\/(\S+)(?:\s+([\s\S]*))?$/.exec(text);
  if (!m) return null;
  const token = m[1].toLowerCase();
  const rest = (m[2] ?? '').trim();
  const skill = ctx.skills.find((s) => s.enabled !== false && s.name.toLowerCase() === token);
  if (skill) return { kind: 'skill', name: skill.name, rest };
  const prompt = ctx.prompts.find((p) => p.command.toLowerCase() === token);
  if (prompt) return { kind: 'prompt', command: prompt.command, args: rest };
  return null;
}

/** Name what the open menu lists, for the popup's heading: `subCmd` is a
 *  slash command whose options are showing (`effort `). */
export function autocompleteHeading(mode: 'slash' | 'at' | null, subCmd: string | null): string {
  if (subCmd) return 'Options';
  return mode === 'at' ? 'Mentions' : 'Commands';
}
