import React, { useCallback, useEffect, useRef, useState } from 'react';
import { get } from '@/api/client';
import {
  AT_ROOT_ENTRIES,
  matchColonSubmenu,
  optLabel,
  optValue,
  SUPPORTED_ATTACHMENT_SUMMARY,
  type ACItem,
  type McpServerLike,
  type SkillLike,
  type SlashCommand,
} from '@/components/chat/chatInputConstants';

export interface UseChatAutocompleteOptions {
  text: string;
  setText: React.Dispatch<React.SetStateAction<string>>;
  textareaRef: React.RefObject<HTMLTextAreaElement | null>;
  /** Already-hydrated slash command list (the /model entry's options are
   *  filled from live config by the caller). */
  slashCommands: SlashCommand[];
  skills: SkillLike[];
  mcpServers: McpServerLike[];
  /** Called when a file is chosen from the `/file:` submenu — attaches it to
   *  the composer as a chip (background upload) instead of inserting text. */
  onAttachWorkspaceFile?: (path: string) => void;
}

export interface UseChatAutocompleteResult {
  acItems: ACItem[];
  acVisible: boolean;
  acIndex: number;
  acRect: { left: number; bottom: number; width: number } | null;
  setAcIndex: React.Dispatch<React.SetStateAction<number>>;
  closeAc: () => void;
  selectAcItem: (item: ACItem) => void;
  computeAutocomplete: (val: string, cursorPos: number) => void;
}

/**
 * Slash-command and @-mention autocomplete state machine for the chat
 * composer. Extracted verbatim from ChatInput.tsx — owns the popup state
 * (items, visibility, selection index, mode, submenu, screen rect) and
 * the text-mutation handlers that insert a chosen completion back into
 * the textarea.
 *
 * Inputs are aliased to their original local names (SLASH_COMMANDS, etc.)
 * so the moved function bodies remain byte-for-byte identical to the
 * original component code.
 */
export function useChatAutocomplete(opts: UseChatAutocompleteOptions): UseChatAutocompleteResult {
  const {
    text,
    setText,
    textareaRef,
    slashCommands: SLASH_COMMANDS,
    skills,
    mcpServers,
    onAttachWorkspaceFile,
  } = opts;

  /* Autocomplete state */
  const [acItems, setAcItems] = useState<ACItem[]>([]);
  const [acVisible, setAcVisible] = useState(false);
  const [acIndex, setAcIndex] = useState(0);
  const [acMode, setAcMode] = useState<'slash' | 'at' | null>(null);
  const [acSubCmd, setAcSubCmd] = useState<string | null>(null);
  const [acRect, setAcRect] = useState<{ left: number; bottom: number; width: number } | null>(null);

  /* ── Autocomplete positioning ── */
  const updateAcRect = useCallback(() => {
    const el = textareaRef.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    setAcRect({ left: rect.left, bottom: window.innerHeight - rect.top, width: rect.width });
  }, [textareaRef]);

  /* ── Close autocomplete ── */
  const closeAc = useCallback(() => {
    setAcVisible(false);
    setAcItems([]);
    setAcIndex(0);
    setAcMode(null);
    setAcSubCmd(null);
  }, []);

  /* ── Insert text from autocomplete selection ── */
  const insertAcText = useCallback((insert: string) => {
    const ta = textareaRef.current;
    if (!ta) return;
    const cursorPos = ta.selectionStart;
    const val = text;

    if (acMode === 'slash') {
      // Replace from the leading / to cursor
      const before = val.slice(0, cursorPos);
      const slashIdx = before.lastIndexOf('/');
      const prefix = slashIdx >= 0 ? val.slice(0, slashIdx) : val.slice(0, cursorPos);
      const after = val.slice(cursorPos);
      const newText = prefix + '/' + insert + after;
      setText(newText);
      // Set cursor after inserted text
      requestAnimationFrame(() => {
        const pos = prefix.length + 1 + insert.length;
        ta.setSelectionRange(pos, pos);
        ta.focus();
      });
    } else if (acMode === 'at') {
      const before = val.slice(0, cursorPos);
      const atIdx = before.lastIndexOf('@');
      const prefix = atIdx >= 0 ? val.slice(0, atIdx) : val.slice(0, cursorPos);
      const after = val.slice(cursorPos);
      const newText = prefix + insert + ' ' + after;
      setText(newText);
      requestAnimationFrame(() => {
        const pos = prefix.length + insert.length + 1;
        ta.setSelectionRange(pos, pos);
        ta.focus();
      });
    }
  }, [text, acMode, setText, textareaRef]);

  /* ── Remove the in-progress slash token (e.g. ``/file:report``) ── */
  // Used after a /file: result is attached as a composer chip, so no command
  // text is left behind in the textarea.
  const removeSlashToken = useCallback(() => {
    const ta = textareaRef.current;
    if (!ta) return;
    const cursorPos = ta.selectionStart;
    const val = text;
    const before = val.slice(0, cursorPos);
    const slashIdx = before.lastIndexOf('/');
    if (slashIdx < 0) return;
    const prefix = val.slice(0, slashIdx);
    const after = val.slice(cursorPos);
    setText(prefix + after);
    requestAnimationFrame(() => {
      ta.setSelectionRange(prefix.length, prefix.length);
      ta.focus();
    });
  }, [text, setText, textareaRef]);

  /* ── Load submenu items for slash commands ── */
  const loadSubmenuItems = useCallback((type: string) => {
    if (type === 'skills') {
      const items: ACItem[] = skills.map((s) => ({
        icon: '⚡',
        name: s.name,
        desc: s.description ?? 'Skill',
        insert: 'skills:' + s.name,
      }));
      setAcItems(items.length > 0 ? items : [{ icon: '⚡', name: 'No skills', desc: 'No skills loaded', insert: 'skills:' }]);
      setAcIndex(0);
    } else if (type === 'mcp') {
      const items: ACItem[] = mcpServers.map((s) => ({
        icon: '🔌',
        name: s.name,
        desc: `MCP server (${s.status})`,
        insert: 'mcp:' + s.name,
      }));
      setAcItems(items.length > 0 ? items : [{ icon: '🔌', name: 'No servers', desc: 'No MCP servers', insert: 'mcp:' }]);
      setAcIndex(0);
    } else if (type === 'file') {
      setAcItems([{ icon: '📂', name: 'Type to search...', desc: `Supported: ${SUPPORTED_ATTACHMENT_SUMMARY}`, insert: 'file:' }]);
      setAcIndex(0);
    }
  }, [skills, mcpServers]);

  /* ── Load submenu items for @ mentions ── */
  const loadAtSubmenuItems = useCallback((type: string) => {
    if (type === 'skills') {
      const items: ACItem[] = skills.map((s) => ({
        icon: '⚡',
        name: s.name,
        desc: s.description ?? 'Skill',
        insert: '@skills:' + s.name,
      }));
      setAcItems(items.length > 0 ? items : [{ icon: '⚡', name: 'No skills', desc: 'No skills loaded', insert: '@skills:' }]);
      setAcIndex(0);
    } else if (type === 'mcp') {
      const items: ACItem[] = mcpServers.map((s) => ({
        icon: '🔌',
        name: s.name,
        desc: `MCP server (${s.status})`,
        insert: '@mcp:' + s.name,
      }));
      setAcItems(items.length > 0 ? items : [{ icon: '🔌', name: 'No servers', desc: 'No MCP servers', insert: '@mcp:' }]);
      setAcIndex(0);
    } else if (type === 'file') {
      setAcItems([{ icon: '📂', name: 'Type to search...', desc: `Supported: ${SUPPORTED_ATTACHMENT_SUMMARY}`, insert: '@file:' }]);
      setAcIndex(0);
    }
  }, [skills, mcpServers]);

  /* ── Select an autocomplete item ── */
  const selectAcItem = useCallback((item: ACItem) => {
    // A real file result from the /file: submenu (icon '📄', as opposed to the
    // '📂' placeholder/no-result rows): attach it to the composer as a chip
    // and strip the /file: token instead of inserting command text. The chip
    // uploads in the background; the user then types a message and submits.
    if (acMode === 'slash' && acSubCmd === 'file:' && item.icon === '📄') {
      const path = item.insert.startsWith('file:') ? item.insert.slice('file:'.length) : item.name;
      onAttachWorkspaceFile?.(path);
      removeSlashToken();
      closeAc();
      return;
    }

    // Check if this is a slash command with options (sub-items)
    if (acMode === 'slash' && !acSubCmd) {
      const cmd = SLASH_COMMANDS.find((c) => '/' + c.cmd === item.insert || item.insert === c.cmd);
      if (cmd?.options) {
        // Show options as sub-items. Display the label but insert the value
        // (they differ for /model: label "Sonnet 4.6" vs value "sonnet").
        const subItems: ACItem[] = cmd.options.map((opt) => ({
          icon: cmd.icon,
          name: optLabel(opt),
          desc: `${cmd.cmd.trim()} ${optValue(opt)}`,
          insert: cmd.cmd + optValue(opt),
        }));
        setAcSubCmd(cmd.cmd);
        setAcItems(subItems);
        setAcIndex(0);
        // Update the text to show the command so far
        const ta = textareaRef.current;
        if (ta) {
          const cursorPos = ta.selectionStart;
          const before = text.slice(0, cursorPos);
          const slashIdx = before.lastIndexOf('/');
          const prefix = slashIdx >= 0 ? text.slice(0, slashIdx) : text.slice(0, cursorPos);
          const after = text.slice(cursorPos);
          const newText = prefix + '/' + cmd.cmd + after;
          setText(newText);
          requestAnimationFrame(() => {
            const pos = prefix.length + 1 + cmd.cmd.length;
            ta.setSelectionRange(pos, pos);
            ta.focus();
          });
        }
        return;
      }
      if (cmd?.submenu) {
        // Show submenu items (skills, files, mcp)
        const cmdName = cmd.cmd.replace(':', '');
        setAcSubCmd(cmd.cmd);
        loadSubmenuItems(cmdName);
        // Update text
        const ta = textareaRef.current;
        if (ta) {
          const cursorPos = ta.selectionStart;
          const before = text.slice(0, cursorPos);
          const slashIdx = before.lastIndexOf('/');
          const prefix = slashIdx >= 0 ? text.slice(0, slashIdx) : text.slice(0, cursorPos);
          const after = text.slice(cursorPos);
          const newText = prefix + '/' + cmd.cmd + after;
          setText(newText);
          requestAnimationFrame(() => {
            const pos = prefix.length + 1 + cmd.cmd.length;
            ta.setSelectionRange(pos, pos);
            ta.focus();
          });
        }
        return;
      }
    }

    // Check if this is an @ mention with submenu
    if (acMode === 'at' && !acSubCmd) {
      if (item.insert === '@file:' || item.insert === '@skills:' || item.insert === '@mcp:') {
        const subType = item.insert.replace('@', '').replace(':', '');
        setAcSubCmd(item.insert);
        loadAtSubmenuItems(subType);
        // Update text
        const ta = textareaRef.current;
        if (ta) {
          const cursorPos = ta.selectionStart;
          const before = text.slice(0, cursorPos);
          const atIdx = before.lastIndexOf('@');
          const prefix = atIdx >= 0 ? text.slice(0, atIdx) : text.slice(0, cursorPos);
          const after = text.slice(cursorPos);
          const newText = prefix + item.insert + after;
          setText(newText);
          requestAnimationFrame(() => {
            const pos = prefix.length + item.insert.length;
            ta.setSelectionRange(pos, pos);
            ta.focus();
          });
        }
        return;
      }
    }

    insertAcText(item.insert);
    closeAc();
  }, [acMode, acSubCmd, text, setText, textareaRef, insertAcText, closeAc, loadSubmenuItems, loadAtSubmenuItems, SLASH_COMMANDS, onAttachWorkspaceFile, removeSlashToken]);


  /* ── File search for @file: and /file: ── */
  const fileSearchTimeout = useRef<ReturnType<typeof setTimeout> | null>(null);
  // Monotonic request id: each searchFiles() call claims the next id, and any
  // async result whose id is no longer current is dropped. Without this a
  // slow response for an earlier query could resolve AFTER a newer query and
  // overwrite the newer results (out-of-order race). Bumped for every call —
  // including the synchronous short-query branch — so starting a new search
  // also invalidates any in-flight fetch.
  const searchReqIdRef = useRef(0);
  const searchFiles = useCallback(async (query: string, prefix: string) => {
    const reqId = ++searchReqIdRef.current;
    if (query.length < 1) {
      setAcItems([{ icon: '📂', name: 'Type to search...', desc: `Supported: ${SUPPORTED_ATTACHMENT_SUMMARY}`, insert: prefix }]);
      return;
    }
    try {
      const data = await get<{ results: Array<{ path: string }>; files?: Array<{ path: string }> }>(`/api/workspace/search-files?q=${encodeURIComponent(query)}`);
      // Stale: a newer search superseded this one while the fetch was in flight.
      if (reqId !== searchReqIdRef.current) return;
      const files = data.results ?? data.files ?? [];
      if (files.length === 0) {
        setAcItems([{ icon: '📂', name: 'No results', desc: `No files matching "${query}"`, insert: prefix + query }]);
      } else {
        setAcItems(files.map((f) => ({
          icon: '📄',
          name: f.path,
          desc: 'Workspace file',
          insert: prefix + f.path,
        })));
      }
      setAcIndex(0);
    } catch {
      if (reqId !== searchReqIdRef.current) return;
      setAcItems([{ icon: '📂', name: 'Search failed', desc: 'Could not search files', insert: prefix + query }]);
    }
  }, []);

  // Clear a pending debounce timer on unmount so a late searchFiles() can't
  // fire (and setState) after the composer has gone away.
  useEffect(() => () => {
    if (fileSearchTimeout.current) clearTimeout(fileSearchTimeout.current);
  }, []);

  /* ── Compute autocomplete on text change ── */
  const computeAutocomplete = useCallback((val: string, cursorPos: number) => {
    const before = val.slice(0, cursorPos);

    // Check for /subcmd:query patterns (e.g. /file:main.ts, /skills:web,
    // /mcp:tool). matchColonSubmenu only matches while a single-token argument
    // is being typed — once a further space starts the message body it returns
    // null, so the search popup dismisses instead of swallowing the question.
    const colonSubMatch = matchColonSubmenu(before);
    if (colonSubMatch) {
      const subType = colonSubMatch.type;
      const subQuery = colonSubMatch.query;
      const cmd = SLASH_COMMANDS.find((c) => c.cmd === subType + ':');
      if (cmd) {
        updateAcRect();
        setAcMode('slash');
        setAcSubCmd(cmd.cmd);
        if (subType === 'file') {
          if (fileSearchTimeout.current) clearTimeout(fileSearchTimeout.current);
          fileSearchTimeout.current = setTimeout(() => {
            void searchFiles(subQuery, 'file:');
          }, 200);
          setAcVisible(true);
          return;
        } else if (subType === 'skills') {
          const filtered = skills.filter((s) => !subQuery || s.name.toLowerCase().includes(subQuery.toLowerCase()));
          setAcItems(filtered.length > 0 ? filtered.map((s) => ({
            icon: '⚡',
            name: s.name,
            desc: s.description ?? 'Skill',
            insert: 'skills:' + s.name,
          })) : [{ icon: '⚡', name: 'No matches', desc: 'No skills found', insert: 'skills:' }]);
          setAcIndex(0);
          setAcVisible(true);
          return;
        } else if (subType === 'mcp') {
          const filtered = mcpServers.filter((s) => !subQuery || s.name.toLowerCase().includes(subQuery.toLowerCase()));
          setAcItems(filtered.length > 0 ? filtered.map((s) => ({
            icon: '🔌',
            name: s.name,
            desc: `MCP server (${s.status})`,
            insert: 'mcp:' + s.name,
          })) : [{ icon: '🔌', name: 'No matches', desc: 'No MCP servers found', insert: 'mcp:' }]);
          setAcIndex(0);
          setAcVisible(true);
          return;
        }
      }
    }

    // Check for slash command at start of input
    const slashMatch = before.match(/^\/(\S*)$/);
    if (slashMatch) {
      const query = slashMatch[1].toLowerCase();
      updateAcRect();
      setAcMode('slash');
      setAcSubCmd(null);

      if (!query) {
        // Show all commands
        const items: ACItem[] = SLASH_COMMANDS.map((c) => ({
          icon: c.icon,
          name: '/' + c.cmd,
          desc: c.desc,
          insert: c.cmd,
        }));
        setAcItems(items);
        setAcIndex(0);
        setAcVisible(true);
        return;
      }

      const filtered = SLASH_COMMANDS.filter((c) => c.cmd.toLowerCase().startsWith(query));
      if (filtered.length > 0) {
        const items: ACItem[] = filtered.map((c) => ({
          icon: c.icon,
          name: '/' + c.cmd,
          desc: c.desc,
          insert: c.cmd,
        }));
        setAcItems(items);
        setAcIndex(0);
        setAcVisible(true);
        return;
      }

      closeAc();
      return;
    }

    // Check for slash command with options: /cmd optionQuery
    const slashOptMatch = before.match(/^\/(\S+)\s+(\S*)$/);
    if (slashOptMatch) {
      const cmdName = slashOptMatch[1].toLowerCase();
      const optQuery = slashOptMatch[2].toLowerCase();
      const cmd = SLASH_COMMANDS.find((c) => c.cmd.trim().toLowerCase() === cmdName);

      if (cmd?.options) {
        updateAcRect();
        setAcMode('slash');
        setAcSubCmd(cmd.cmd);
        // Match either the shown label or the inserted value so typing "son"
        // (or "sonnet") both surface "Sonnet 4.6".
        const filtered = cmd.options.filter(
          (o) =>
            optLabel(o).toLowerCase().startsWith(optQuery) ||
            optValue(o).toLowerCase().startsWith(optQuery),
        );
        if (filtered.length > 0) {
          const items: ACItem[] = filtered.map((opt) => ({
            icon: cmd.icon,
            name: optLabel(opt),
            desc: `${cmd.cmd.trim()} ${optValue(opt)}`,
            insert: cmd.cmd + optValue(opt),
          }));
          setAcItems(items);
          setAcIndex(0);
          setAcVisible(true);
          return;
        }
      }

      if (cmd?.submenu) {
        const subType = cmd.cmd.replace(':', '');
        // A space after the filename means the user has finished picking the
        // file and is now writing their message — dismiss the file search
        // (matchColonSubmenu handles the still-typing-a-filename case above).
        if (subType === 'file') {
          closeAc();
          return;
        }
        updateAcRect();
        setAcMode('slash');
        setAcSubCmd(cmd.cmd);
        if (subType === 'skills') {
          const filtered = skills.filter((s) => s.name.toLowerCase().startsWith(optQuery));
          setAcItems(filtered.map((s) => ({
            icon: '⚡',
            name: s.name,
            desc: s.description ?? 'Skill',
            insert: 'skills:' + s.name,
          })));
          setAcIndex(0);
          setAcVisible(true);
          return;
        } else if (subType === 'mcp') {
          const filtered = mcpServers.filter((s) => s.name.toLowerCase().startsWith(optQuery));
          setAcItems(filtered.map((s) => ({
            icon: '🔌',
            name: s.name,
            desc: `MCP server (${s.status})`,
            insert: 'mcp:' + s.name,
          })));
          setAcIndex(0);
          setAcVisible(true);
          return;
        }
      }

      closeAc();
      return;
    }

    // Check for @ mention
    const atMatch = before.match(/@(\S*)$/);
    if (atMatch) {
      const query = atMatch[1].toLowerCase();
      updateAcRect();
      setAcMode('at');

      // Check for @file:query, @skills:query, @mcp:query
      const subMatch = query.match(/^(file|skills|mcp):(.*)$/);
      if (subMatch) {
        const subType = subMatch[1];
        const subQuery = subMatch[2].toLowerCase();
        setAcSubCmd('@' + subType + ':');

        if (subType === 'file') {
          if (fileSearchTimeout.current) clearTimeout(fileSearchTimeout.current);
          fileSearchTimeout.current = setTimeout(() => {
            void searchFiles(subQuery, '@file:');
          }, 200);
          setAcVisible(true);
          return;
        } else if (subType === 'skills') {
          const filtered = skills.filter((s) => s.name.toLowerCase().includes(subQuery));
          setAcItems(filtered.length > 0 ? filtered.map((s) => ({
            icon: '⚡',
            name: s.name,
            desc: s.description ?? 'Skill',
            insert: '@skills:' + s.name,
          })) : [{ icon: '⚡', name: 'No matches', desc: 'No skills found', insert: '@skills:' }]);
          setAcIndex(0);
          setAcVisible(true);
          return;
        } else if (subType === 'mcp') {
          const filtered = mcpServers.filter((s) => s.name.toLowerCase().includes(subQuery));
          setAcItems(filtered.length > 0 ? filtered.map((s) => ({
            icon: '🔌',
            name: s.name,
            desc: `MCP server (${s.status})`,
            insert: '@mcp:' + s.name,
          })) : [{ icon: '🔌', name: 'No matches', desc: 'No MCP servers found', insert: '@mcp:' }]);
          setAcIndex(0);
          setAcVisible(true);
          return;
        }
      }

      // Root @ mention: show categories + matching skills
      setAcSubCmd(null);
      const rootItems = AT_ROOT_ENTRIES.filter((e) => !query || e.name.toLowerCase().includes(query));
      const skillItems: ACItem[] = skills
        .filter((s) => !query || s.name.toLowerCase().includes(query))
        .map((s) => ({
          icon: '⚡',
          name: '@' + s.name,
          desc: s.description ?? 'Skill',
          insert: '@' + s.name,
        }));
      const allItems = [...rootItems, ...skillItems];
      if (allItems.length > 0) {
        setAcItems(allItems);
        setAcIndex(0);
        setAcVisible(true);
        return;
      }
    }

    closeAc();
  }, [updateAcRect, closeAc, skills, mcpServers, searchFiles, SLASH_COMMANDS]);

  return {
    acItems,
    acVisible,
    acIndex,
    acRect,
    setAcIndex,
    closeAc,
    selectAcItem,
    computeAutocomplete,
  };
}
