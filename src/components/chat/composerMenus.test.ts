import { describe, expect, it } from 'vitest';
import type { SlashCommand } from './chatInputConstants';
import { atItems, parseSlashSend, pathItems, slashItems } from './composerMenus';

const commands: SlashCommand[] = [
  { cmd: 'compact ', icon: 'layers', desc: 'Summarize' },
  { cmd: 'mcp ', icon: 'link', desc: 'Manage MCP servers in Settings' },
];
const skills = [
  { name: 'meeting_notes', description: 'Notes from a meeting' },
  { name: 'compact', description: 'A skill named like a command' },
  { name: 'off_skill', enabled: false },
];
const prompts = [
  {
    command: 'github:pr_review',
    description: 'Review a pull request',
    arguments: [{ name: 'number', required: true }, { name: 'focus', required: false }],
  },
];

describe('the / menu', () => {
  it('lists the commands, then the enabled skills, then the MCP prompts', () => {
    expect(slashItems('', { commands, skills, prompts }).map((i) => [i.name, i.kind])).toEqual([
      ['/compact', 'command'],
      ['/mcp', 'command'],
      ['/meeting_notes', 'skill'],
      ['/github:pr_review', 'prompt'],
    ]);
  });

  it('narrows to what the typed text matches, and shows a prompt\'s arguments', () => {
    const items = slashItems('git', { commands, skills, prompts });

    expect(items).toHaveLength(1);
    expect(items[0]).toMatchObject({ insert: 'github:pr_review ', desc: 'Review a pull request (number [focus])' });
  });
});

describe('the @ menu', () => {
  const ctx = {
    sessions: [{ id: 's1', title: 'Release plan' }],
    agents: [{ name: 'explore', description: 'Read-only' }],
    resources: [{ mention: 'github:issue://12', name: 'Issue 12', description: '' }],
    workspace: true,
  };

  it('offers agents, sessions, resources and the search sources, with a file hint first', () => {
    expect(atItems('', ctx).map((i) => [i.name, i.insert, i.kind])).toEqual([
      ['Type to search files and folders', '', 'hint'],
      ['@agent-explore', '@agent-explore', 'agent'],
      ['@Release plan', '@release_plan', 'session'],
      ['@github:issue://12', '@github:issue://12', 'resource'],
      ['@index', '@index', 'source'],
      ['@docs', '@docs', 'source'],
    ]);
  });

  it('narrows every kind by the typed text and drops the hint', () => {
    expect(atItems('issue', ctx).map((i) => i.name)).toEqual(['@github:issue://12']);
    expect(atItems('agent', ctx).map((i) => i.name)).toEqual(['@agent-explore']);
  });

  it('shows no file hint without a workspace', () => {
    expect(atItems('', { ...ctx, workspace: false })[0].kind).toBe('agent');
  });

  it('turns search results into file and folder rows that insert nothing', () => {
    expect(pathItems([{ path: 'src', folder: true }, { path: 'src/app.ts' }])).toEqual([
      expect.objectContaining({ name: 'src/', kind: 'folder', path: 'src', insert: '' }),
      expect.objectContaining({ name: 'src/app.ts', kind: 'file', path: 'src/app.ts', insert: '' }),
    ]);
  });
});

describe('a sent / message', () => {
  const ctx = { skills, prompts };

  it('runs an enabled skill with the text after it', () => {
    expect(parseSlashSend('/Meeting_Notes tidy these', ctx)).toEqual({ kind: 'skill', name: 'meeting_notes', rest: 'tidy these' });
    expect(parseSlashSend('/meeting_notes', ctx)).toEqual({ kind: 'skill', name: 'meeting_notes', rest: '' });
  });

  it('runs an MCP prompt with the words after it as its arguments', () => {
    expect(parseSlashSend('/github:pr_review 456 the auth', ctx)).toEqual({
      kind: 'prompt',
      command: 'github:pr_review',
      args: '456 the auth',
    });
  });

  it('is neither for a disabled skill, an unknown name or plain text', () => {
    expect(parseSlashSend('/off_skill now', ctx)).toBeNull();
    expect(parseSlashSend('/nothing here', ctx)).toBeNull();
    expect(parseSlashSend('a /meeting_notes later', ctx)).toBeNull();
  });
});
