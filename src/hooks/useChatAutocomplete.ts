import React, { useCallback, useEffect, useRef, useState } from 'react';
import { get } from '@/api/client';
import { optLabel, optValue, type SessionLike, type SlashCommand } from '@/components/chat/chatInputConstants';
import {
  atItems,
  autocompleteHeading,
  pathItems,
  slashItems,
  type ACItem,
  type AgentTypeLike,
  type McpPromptLike,
  type McpResourceLike,
  type SkillLike,
} from '@/components/chat/composerMenus';

export interface UseChatAutocompleteOptions {
  text: string;
  setText: React.Dispatch<React.SetStateAction<string>>;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  /** Already-hydrated slash command list (the /model entry's options are
   *  filled from live config by the caller). */
  slashCommands: SlashCommand[];
  skills: SkillLike[];
  /** The prompts of the connected MCP servers (`/server:prompt`). */
  mcpPrompts: McpPromptLike[];
  /** The resources of the connected MCP servers (`@server:uri`). */
  mcpResources: McpResourceLike[];
  /** Other chat sessions, mentionable as a bare `@<slug>` to pull that
   *  session's recent conversation in as context (resolved server-side). */
  sessions: SessionLike[];
  /** The agent types the user can name as `@agent-<type>`. */
  agents: AgentTypeLike[];
  /** Whether a workspace is connected, so `@` searches its files. */
  workspace: boolean;
  /** A file or folder picked from the `@` menu: attach it as a chip. */
  onAttachWorkspacePath: (path: string, folder: boolean) => void;
}

export interface UseChatAutocompleteResult {
  acItems: ACItem[];
  acVisible: boolean;
  acIndex: number;
  /** What the open menu lists ("Commands", "Mentions", "Options"), shown as
   *  the popup's heading. */
  acHeading: string;
  acRect: { left: number; bottom: number; width: number } | null;
  setAcIndex: React.Dispatch<React.SetStateAction<number>>;
  closeAc: () => void;
  selectAcItem: (item: ACItem) => void;
  computeAutocomplete: (val: string, cursorPos: number) => void;
}

/** A search runs this long after the last keystroke. */
const SEARCH_DEBOUNCE_MS = 120;
/** The most files and folders one `@` search lists. */
const PATH_RESULTS = 50;

/**
 * The composer's `/` and `@` menus (src/components/chat/composerMenus.ts):
 * owns the popup state (items, visibility, selection index, mode, the
 * command whose options show, screen rect) and the text changes a pick makes.
 */
export function useChatAutocomplete(opts: UseChatAutocompleteOptions): UseChatAutocompleteResult {
  const {
    text,
    setText,
    textareaRef,
    slashCommands,
    skills,
    mcpPrompts,
    mcpResources,
    sessions,
    agents,
    workspace,
    onAttachWorkspacePath,
  } = opts;

  const [acItems, setAcItems] = useState<ACItem[]>([]);
  const [acVisible, setAcVisible] = useState(false);
  const [acIndex, setAcIndex] = useState(0);
  const [acMode, setAcMode] = useState<'slash' | 'at' | null>(null);
  const [acSubCmd, setAcSubCmd] = useState<string | null>(null);
  const [acRect, setAcRect] = useState<{ left: number; bottom: number; width: number } | null>(null);

  const updateAcRect = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    setAcRect({ left: rect.left, bottom: window.innerHeight - rect.top, width: rect.width });
  }, [textareaRef]);

  // Monotonic request id: each path search claims the next id, and a result
  // whose id is no longer current is dropped, so a slow answer for an earlier
  // query never overwrites a newer one. Closing the menu bumps it too.
  const searchReqIdRef = useRef(0);
  const searchTimer = useRef<ReturnType<typeof setTimeout> | null>(null);
  useEffect(() => () => {
    if (searchTimer.current) clearTimeout(searchTimer.current);
  }, []);

  const closeAc = useCallback(() => {
    searchReqIdRef.current++;
    setAcVisible(false);
    setAcItems([]);
    setAcIndex(0);
    setAcMode(null);
    setAcSubCmd(null);
  }, []);

  const show = useCallback((mode: 'slash' | 'at', subCmd: string | null, items: ACItem[]) => {
    updateAcRect();
    setAcMode(mode);
    setAcSubCmd(subCmd);
    setAcItems(items);
    setAcIndex(0);
    setAcVisible(items.length > 0);
  }, [updateAcRect]);

  /** Files and folders for an `@` query, ahead of the other mentions. */
  const searchPaths = useCallback((query: string, others: ACItem[]) => {
    const reqId = ++searchReqIdRef.current;
    if (searchTimer.current) clearTimeout(searchTimer.current);
    searchTimer.current = setTimeout(async () => {
      try {
        const data = await get<{ results: Array<{ path: string; folder?: boolean }> }>(
          `/api/workspace/search-files?q=${encodeURIComponent(query)}&folders=true&limit=${PATH_RESULTS}`,
        );
        if (reqId !== searchReqIdRef.current) return;
        const items = [...pathItems(data.results ?? []), ...others];
        setAcItems(items);
        setAcIndex(0);
        setAcVisible(items.length > 0);
      } catch {
        // No search result: the other mentions stay listed.
      }
    }, SEARCH_DEBOUNCE_MS);
  }, []);

  const optionItems = useCallback((cmd: SlashCommand, query: string): ACItem[] => (cmd.options ?? [])
    // Match either the shown label or the inserted value, so typing "son"
    // (or "sonnet5") both surface "Sonnet 5.5".
    .filter((o) => optLabel(o).toLowerCase().startsWith(query) || optValue(o).toLowerCase().startsWith(query))
    .map((opt) => ({
      icon: cmd.icon,
      name: optLabel(opt),
      desc: `${cmd.cmd.trim()} ${optValue(opt)}`,
      insert: cmd.cmd + optValue(opt),
      kind: 'option',
    })), []);

  const computeAutocomplete = useCallback((val: string, cursorPos: number) => {
    const before = val.slice(0, cursorPos);

    // `/` and a command, skill or prompt name, at the start of the message.
    const slash = before.match(/^\/(\S*)$/);
    if (slash) {
      searchReqIdRef.current++;
      show('slash', null, slashItems(slash[1], { commands: slashCommands, skills, prompts: mcpPrompts }));
      return;
    }

    // The options of a command: `/effort hi`.
    const option = before.match(/^\/(\S+)\s+(\S*)$/);
    if (option) {
      const cmd = slashCommands.find((c) => c.cmd.trim().toLowerCase() === option[1].toLowerCase());
      if (cmd?.options) {
        searchReqIdRef.current++;
        show('slash', cmd.cmd, optionItems(cmd, option[2].toLowerCase()));
        return;
      }
    }

    // `@` at the start of the message or after a space ("a@b" is an address).
    const at = before.match(/(?:^|\s)@(\S*)$/);
    if (at) {
      const others = atItems(at[1], { sessions, agents, resources: mcpResources, workspace });
      show('at', null, others);
      if (workspace && at[1]) searchPaths(at[1], others);
      else searchReqIdRef.current++;
      return;
    }

    closeAc();
  }, [show, closeAc, searchPaths, optionItems, slashCommands, skills, mcpPrompts, sessions, agents, mcpResources, workspace]);

  /** Replace the token being typed (`/name` or `@name`) with `insert`. */
  const replaceToken = useCallback((insert: string) => {
    const ta = textareaRef.current;
    if (!ta) return;
    const cursorPos = ta.selectionStart;
    const before = text.slice(0, cursorPos);
    const marker = acMode === 'slash' ? '/' : '@';
    const start = before.lastIndexOf(marker);
    const prefix = start >= 0 ? text.slice(0, start) : before;
    const replacement = acMode === 'slash' ? '/' + insert : insert ? insert + ' ' : '';
    setText(prefix + replacement + text.slice(cursorPos));
    requestAnimationFrame(() => {
      const pos = prefix.length + replacement.length;
      ta.setSelectionRange(pos, pos);
      ta.focus();
    });
  }, [text, acMode, setText, textareaRef]);

  const selectAcItem = useCallback((item: ACItem) => {
    if (item.kind === 'hint') return;

    // A file or folder becomes a chip: the `@query` text goes away.
    if ((item.kind === 'file' || item.kind === 'folder') && item.path) {
      onAttachWorkspacePath(item.path, item.kind === 'folder');
      replaceToken('');
      closeAc();
      return;
    }

    // A command with options shows them next.
    if (item.kind === 'command') {
      const cmd = slashCommands.find((c) => c.cmd === item.insert);
      if (cmd?.options) {
        replaceToken(cmd.cmd);
        show('slash', cmd.cmd, optionItems(cmd, ''));
        return;
      }
    }

    replaceToken(item.insert);
    closeAc();
  }, [onAttachWorkspacePath, replaceToken, closeAc, show, optionItems, slashCommands]);

  // Derived from the menu state rather than stored, so the heading can never
  // disagree with the menu it names.
  const acHeading = autocompleteHeading(acMode, acSubCmd);

  return {
    acItems,
    acVisible,
    acIndex,
    acHeading,
    acRect,
    setAcIndex,
    closeAc,
    selectAcItem,
    computeAutocomplete,
  };
}
