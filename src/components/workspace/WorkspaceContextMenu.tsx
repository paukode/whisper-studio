import React, { useCallback, useState, useRef, useEffect } from 'react';
import { ContextMenu, type MenuItem } from '@/components/common/ContextMenu';
import { useWorkspaceStore } from '@/stores/workspaceStore';
import { useUIStore } from '@/stores/uiStore';
import { deleteFile, writeFile, listDir, queryFile, rawFileUrl } from '@/api/workspace';
import { get, post } from '@/api/client';
import { toError } from '@/utils/toError';

export interface ContextMenuState {
  x: number;
  y: number;
  path: string;
  type: 'file' | 'directory';
}

export interface WorkspaceContextMenuProps {
  state: ContextMenuState;
  onClose: () => void;
  onFileSelect: (path: string) => void;
}

const isMac = typeof navigator !== 'undefined' && /Mac/.test(navigator.userAgent);
const MOD = isMac ? '\u2318' : 'Ctrl+';
const OPT = isMac ? '\u2325' : 'Alt+';
const SHIFT = isMac ? '\u21E7' : 'Shift+';

// ── Module-level state for file clipboard and comparison ──

interface FileClipboard {
  operation: 'cut' | 'copy';
  paths: string[];
}

let _clipboard: FileClipboard | null = null;
let _compareSelection: string | null = null;

/** Render a grep-style result list as a dialog body. Dialog bodies are React
 *  nodes (not HTML strings), so building JSX here both renders correctly and
 *  auto-escapes the paths/snippets — no manual HTML escaping needed. Shared by
 *  Find File References and the in-folder search. */
function renderRefList(results: Array<{ path: string; line: number; text: string }>): React.ReactNode {
  return (
    <div style={{ maxHeight: '50vh', overflow: 'auto', fontSize: 12 }}>
      {results.map((r, i) => (
        <div key={`${r.path}:${r.line}:${i}`} style={{ padding: '4px 0', borderBottom: '1px solid var(--border)' }}>
          <code style={{ color: 'var(--accent)' }}>{r.path}:{r.line}</code>{' '}
          <span style={{ color: 'var(--text-muted)' }}>{r.text.trim()}</span>
        </div>
      ))}
    </div>
  );
}

/** Escape regex metacharacters so a literal filename (which may contain
 *  '.', '(', ')', spaces, etc. — e.g. "console_output (10).log") can be used
 *  as a ripgrep pattern without producing a malformed/expansive regex. */
function escapeRegExp(str: string): string {
  return str.replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
}

/**
 * Full context menu for workspace file tree matching the original vanilla JS.
 *
 * File menu (from screenshot): Open, Open Preview, Open With...,
 *   Select for Compare, Find File References, Open Timeline,
 *   Add File to Chat, Cut, Copy, Copy Path, Copy Relative Path,
 *   Rename, Duplicate, Delete, Reveal in Finder.
 *
 * Folder menu (from screenshot): New File..., New Folder...,
 *   Find in Folder..., Add Folder to Chat, Paste,
 *   Copy Path, Copy Relative Path, Rename..., Delete,
 *   Reveal in Finder.
 */
export const WorkspaceContextMenu: React.FC<WorkspaceContextMenuProps> = ({
  state,
  onClose,
  onFileSelect,
}) => {
  const [mode, setMode] = useState<'menu' | 'new-file' | 'new-folder'>('menu');
  const [inputValue, setInputValue] = useState('');
  const inputRef = useRef<HTMLInputElement>(null);
  const confirmedRef = useRef(false);

  const { path, type } = state;
  const fileName = path.split('/').pop() ?? path;
  const parentDir = path.includes('/') ? path.slice(0, path.lastIndexOf('/')) : '.';


  const refreshTree = useCallback(async () => {
    try {
      const rootEntries = await listDir('.');
      // Merge (not replace) so expanded folders keep their lazily-loaded
      // children after a delete/duplicate/paste/new-file refresh.
      useWorkspaceStore.getState().mergeFileTree(rootEntries);
    } catch { /* ignore */ }
  }, []);

  // ═══════════════════════════════════════════════
  // Shared actions
  // ═══════════════════════════════════════════════

  const handleOpen = useCallback(() => {
    if (type === 'file') onFileSelect(path);
    onClose();
  }, [type, path, onFileSelect, onClose]);

  const handleDelete = useCallback(async () => {
    onClose();
    if (!confirm(`Delete ${fileName}?`)) return;
    try {
      await deleteFile(path);
      const tabs = useWorkspaceStore.getState().editorTabs;
      if (tabs.some((t) => t.path === path)) {
        // Prompts to discard first when the open tab has unsaved edits.
        await useWorkspaceStore.getState().confirmCloseTab(path);
      }
      await refreshTree();
      useUIStore.getState().addToast({ type: 'success', message: `Deleted ${fileName}`, duration: 2000 });
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
  }, [path, fileName, refreshTree, onClose]);

  // ── Rename ──

  const handleRenameStart = useCallback(() => {
    onClose();
    // Dispatch event so FileTree shows inline rename input at the correct tree node
    window.dispatchEvent(new CustomEvent('whisper-rename-file', { detail: { path } }));
  }, [path, onClose]);


  // ── New File / New Folder ──

  const handleNewFileStart = useCallback(() => {
    setMode('new-file');
    setInputValue('');
  }, []);

  const handleNewFolderStart = useCallback(() => {
    setMode('new-folder');
    setInputValue('');
  }, []);

  // Re-arm the double-submit guard whenever a create flow starts. Done in an
  // effect (not in the handlers) so the menu-item handlers don't read a ref —
  // which the React Compiler flags when they're passed into the items array.
  useEffect(() => {
    if (mode === 'new-file' || mode === 'new-folder') confirmedRef.current = false;
  }, [mode]);

  const handleCreateConfirm = useCallback(async () => {
    if (confirmedRef.current) return;
    confirmedRef.current = true;
    const name = inputValue.trim();
    if (!name) { onClose(); return; }
    const dir = type === 'directory' ? path : parentDir;
    const newPath = dir === '.' ? name : `${dir}/${name}`;
    try {
      if (mode === 'new-folder') {
        await post('/api/workspace/mkdir', { path: newPath });
      } else {
        await writeFile(newPath, '');
      }
      await refreshTree();
      if (mode === 'new-file') onFileSelect(newPath);
      useUIStore.getState().addToast({ type: 'success', message: `Created ${name}`, duration: 2000 });
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
    onClose();
  }, [mode, inputValue, type, path, parentDir, refreshTree, onFileSelect, onClose]);

  // ── Copy Path / Copy Relative Path ──

  const handleCopyPath = useCallback(() => {
    const wsRoot = useUIStore.getState().wsPath;
    const absolutePath = wsRoot ? `${wsRoot}/${path}` : path;
    void navigator.clipboard.writeText(absolutePath);
    useUIStore.getState().addToast({ type: 'success', message: 'Path copied', duration: 1500 });
    onClose();
  }, [path, onClose]);

  const handleCopyRelativePath = useCallback(() => {
    void navigator.clipboard.writeText(path);
    useUIStore.getState().addToast({ type: 'success', message: 'Relative path copied', duration: 1500 });
    onClose();
  }, [path, onClose]);

  // ── Duplicate ──

  const handleDuplicate = useCallback(async () => {
    try {
      await post('/api/workspace/duplicate', { path });
      await refreshTree();
      useUIStore.getState().addToast({ type: 'success', message: `Duplicated ${fileName}`, duration: 2000 });
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
    onClose();
  }, [path, fileName, refreshTree, onClose]);

  // ── Reveal in Finder ──

  const handleRevealInFinder = useCallback(() => {
    // ``open -R`` is a benign macOS action that reveals the file in
    // Finder. It passes _validate_command (no destructive patterns)
    // so it no longer needs a user_approved bypass — that bypass was
    // a client-asserted boolean and got removed in the audit followup
    // because prompt-injected pages could fabricate it.
    void post('/api/workspace/shell', { command: `open -R "${path}"` }).catch(() => {});
    onClose();
  }, [path, onClose]);

  // ═══════════════════════════════════════════════
  // File-specific actions
  // ═══════════════════════════════════════════════

  /** Open Preview — show file content in a preview dialog. */
  const handleOpenPreview = useCallback(async () => {
    onClose();
    try {
      const data = await queryFile(path);
      if ('binary' in data && data.binary) {
        const info = data as { type: string };
        if (info.type === 'image') {
          const url = rawFileUrl(path);
          useUIStore.getState().pushDialog({
            kind: 'open',
            title: fileName,
            size: 'lg',
            body: (
              <div style={{ textAlign: 'center', padding: 16 }}>
                <img src={url} style={{ maxWidth: '100%', maxHeight: '70vh' }} alt={fileName} />
              </div>
            ),
          });
        } else {
          useUIStore.getState().addToast({ type: 'info', message: 'Binary file: cannot preview', duration: 2000 });
        }
      } else {
        const content = 'content' in data ? (data.content as string) : '';
        useUIStore.getState().pushDialog({
          kind: 'open',
          title: fileName,
          size: 'lg',
          body: (
            <pre style={{ whiteSpace: 'pre-wrap', wordBreak: 'break-word', maxHeight: '70vh', overflow: 'auto', fontSize: 12 }}>
              {content}
            </pre>
          ),
        });
      }
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
  }, [path, fileName, onClose]);

  /** Open With... — delegate to OS via backend. */
  const handleOpenWith = useCallback(async () => {
    onClose();
    try {
      await post('/api/workspace/open-with', { path });
    } catch {
      useUIStore.getState().addToast({ type: 'error', message: 'Open With not available', duration: 2000 });
    }
  }, [path, onClose]);

  /** Select for Compare — store file path for later comparison. The actual
   *  diff opens when the user picks "Compare with Selected" on a 2nd file. */
  const handleSelectForCompare = useCallback(() => {
    _compareSelection = path;
    useUIStore.getState().addToast({
      type: 'info',
      message: `Selected "${fileName}". Now right-click another file and choose "Compare with Selected".`,
      duration: 4000,
    });
    onClose();
  }, [path, fileName, onClose]);

  /** Compare with Selected — open a real side-by-side diff of the two files
   *  in a single editor tab. */
  const handleCompareWithSelected = useCallback(async () => {
    onClose();
    if (!_compareSelection) return;
    const pathA = _compareSelection;
    const pathB = path;
    _compareSelection = null;
    try {
      const [dataA, dataB] = await Promise.all([queryFile(pathA), queryFile(pathB)]);
      const contentA = 'content' in dataA ? (dataA.content as string) : '';
      const contentB = 'content' in dataB ? (dataB.content as string) : '';
      // Language for highlighting — use the right-hand file's extension.
      const lang = pathB.split('.').pop() ?? 'plaintext';
      useWorkspaceStore.getState().openDiffTab(pathA, contentA, pathB, contentB, lang);
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
  }, [path, onClose]);

  /** Find references to this file — grep the workspace for the filename
   *  (literal match: the name is regex-escaped so dots/parens/spaces don't
   *  break the ripgrep pattern). */
  const handleFindFileReferences = useCallback(async () => {
    onClose();
    try {
      const data = await get<{ results?: Array<{ path: string; line: number; text: string }> }>(
        `/api/workspace/grep?pattern=${encodeURIComponent(escapeRegExp(fileName))}&scope=.&limit=50`,
      );
      const results = data.results ?? [];
      if (results.length === 0) {
        useUIStore.getState().addToast({ type: 'info', message: `No references found for ${fileName}`, duration: 2000 });
        return;
      }
      useUIStore.getState().pushDialog({
        kind: 'open',
        title: `References: ${fileName}`,
        size: 'lg',
        body: renderRefList(results),
      });
    } catch {
      useUIStore.getState().addToast({ type: 'error', message: 'Find references not available', duration: 2000 });
    }
  }, [fileName, onClose]);

  /** Open Timeline — show git file history. */
  const handleOpenTimeline = useCallback(async () => {
    onClose();
    try {
      const data = await get<{ history?: Array<{ message: string; date: string; hash: string; author: string }> }>(
        `/api/workspace/file-history?path=${encodeURIComponent(path)}&limit=30`,
      );
      const history = data.history ?? [];
      if (history.length === 0) {
        useUIStore.getState().addToast({ type: 'info', message: 'No git history for this file', duration: 2000 });
        return;
      }
      const body = (
        <div style={{ maxHeight: '50vh', overflow: 'auto', fontSize: 12 }}>
          {history.map((h) => (
            <div
              key={h.hash}
              style={{ padding: '8px 0', borderBottom: '1px solid var(--border)', display: 'flex', justifyContent: 'space-between' }}
            >
              <div>
                <span style={{ fontWeight: 500 }}>{h.message}</span>
                <br />
                <span style={{ fontSize: 10, color: 'var(--text-muted)' }}>
                  {h.hash.slice(0, 8)} · {h.author}
                </span>
              </div>
              <span style={{ color: 'var(--text-muted)', fontSize: 11, whiteSpace: 'nowrap', marginLeft: 12 }}>
                {h.date}
              </span>
            </div>
          ))}
        </div>
      );
      useUIStore.getState().pushDialog({
        kind: 'open',
        title: `Timeline: ${fileName}`,
        size: 'lg',
        body,
      });
    } catch {
      useUIStore.getState().addToast({ type: 'error', message: 'Timeline not available', duration: 2000 });
    }
  }, [path, fileName, onClose]);

  /** Add File to Chat — dispatch event for ChatInput to pick up. */
  const handleAddFileToChat = useCallback(async () => {
    onClose();
    try {
      const data = await queryFile(path);
      if ('binary' in data && data.binary) {
        const info = data as { type: string };
        const supportedBinaryTypes = ['image', 'pdf', 'word', 'spreadsheet', 'presentation'];
        if (supportedBinaryTypes.includes(info.type)) {
          // Fetch raw blob and send as File for upload (images, PDFs, Office docs, etc.)
          const url = rawFileUrl(path);
          const resp = await fetch(url);
          const blob = await resp.blob();
          const file = new File([blob], fileName, { type: blob.type || 'application/octet-stream' });
          window.dispatchEvent(new CustomEvent('whisper-add-to-chat', { detail: { files: [file] } }));
          useUIStore.getState().addToast({ type: 'success', message: `Added ${fileName} to chat`, duration: 2000 });
        } else {
          useUIStore.getState().addToast({ type: 'warning', message: 'Binary file cannot be added to chat', duration: 2000 });
        }
        return;
      }
      const content = 'content' in data ? (data.content as string) : '';
      const file = new File([content], fileName, { type: 'text/plain' });
      window.dispatchEvent(new CustomEvent('whisper-add-to-chat', { detail: { files: [file] } }));
      useUIStore.getState().addToast({ type: 'success', message: `Added ${fileName} to chat`, duration: 2000 });
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
  }, [path, fileName, onClose]);

  // ═══════════════════════════════════════════════
  // Folder-specific actions
  // ═══════════════════════════════════════════════

  /** Add Folder to Chat — add all text files in directory to chat. */
  const handleAddFolderToChat = useCallback(async () => {
    onClose();
    try {
      const entries = await listDir(path);
      const textFiles = entries.filter((e) => e.type === 'file');
      if (textFiles.length === 0) {
        useUIStore.getState().addToast({ type: 'info', message: 'No files in folder', duration: 2000 });
        return;
      }
      const limit = Math.min(textFiles.length, 20);
      const fetches = textFiles.slice(0, limit).map(async (e) => {
        try {
          const data = await queryFile(e.path);
          if ('binary' in data && data.binary) return null;
          const content = 'content' in data ? (data.content as string) : '';
          return new File([content], e.name, { type: 'text/plain' });
        } catch { return null; }
      });
      const files = (await Promise.all(fetches)).filter(Boolean) as File[];
      if (files.length > 0) {
        window.dispatchEvent(new CustomEvent('whisper-add-to-chat', { detail: { files } }));
        useUIStore.getState().addToast({ type: 'success', message: `Added ${files.length} file(s) to chat`, duration: 2000 });
      }
    } catch (err) {
      useUIStore.getState().addToast({ type: 'error', message: toError(err).message });
    }
  }, [path, onClose]);

  /** Find in Folder — search dialog with grep API. */
  const handleFindInFolder = useCallback(async () => {
    onClose();
    const result = await new Promise<unknown>((resolve) => {
      useUIStore.getState().pushDialog({
        kind: 'form',
        title: `Find in: ${path}`,
        fields: [{ name: 'pattern', label: 'Search pattern', type: 'text', placeholder: 'Search pattern...', required: true }],
        confirmText: 'Search',
        _resolve: resolve,
      });
    }) as Record<string, string | boolean> | null;
    if (!result || !result.pattern) return;
    try {
      const data = await get<{ results?: Array<{ path: string; line: number; text: string }> }>(
        `/api/workspace/grep?pattern=${encodeURIComponent(String(result.pattern))}&scope=${encodeURIComponent(path)}&limit=50`,
      );
      const results = data.results ?? [];
      if (results.length === 0) {
        useUIStore.getState().addToast({ type: 'info', message: 'No results found', duration: 2000 });
        return;
      }
      useUIStore.getState().pushDialog({
        kind: 'open',
        title: `Results in ${path}`,
        size: 'lg',
        body: renderRefList(results),
      });
    } catch {
      useUIStore.getState().addToast({ type: 'error', message: 'Search not available', duration: 2000 });
    }
  }, [path, onClose]);

  // ═══════════════════════════════════════════════
  // Cut / Copy / Paste
  // ═══════════════════════════════════════════════

  const handleCut = useCallback(() => {
    _clipboard = { operation: 'cut', paths: [path] };
    window.dispatchEvent(new CustomEvent('workspace-clipboard-change', { detail: _clipboard }));
    useUIStore.getState().addToast({ type: 'info', message: `Cut: ${fileName}`, duration: 1500 });
    onClose();
  }, [path, fileName, onClose]);

  const handleCopyFile = useCallback(() => {
    _clipboard = { operation: 'copy', paths: [path] };
    window.dispatchEvent(new CustomEvent('workspace-clipboard-change', { detail: _clipboard }));
    useUIStore.getState().addToast({ type: 'info', message: `Copied: ${fileName}`, duration: 1500 });
    onClose();
  }, [path, fileName, onClose]);

  const handlePaste = useCallback(async () => {
    onClose();
    if (!_clipboard || _clipboard.paths.length === 0) return;
    const destDir = type === 'directory' ? path : parentDir;
    const endpoint = _clipboard.operation === 'cut' ? '/api/workspace/move' : '/api/workspace/copy-file';
    // Track per-item outcomes so a partial failure in a multi-file paste
    // doesn't get summarised as "Pasted successfully". Use Promise.allSettled
    // so a single failure stops one item, not the whole batch.
    const results = await Promise.allSettled(
      _clipboard.paths.map((srcPath) => post(endpoint, { source: srcPath, destination_dir: destDir })),
    );
    const failures = results.filter((r): r is PromiseRejectedResult => r.status === 'rejected');
    const successes = results.length - failures.length;
    // Cut semantics — only clear the clipboard if every move succeeded; a
    // partial failure leaves the user able to retry without re-cutting.
    if (_clipboard.operation === 'cut' && failures.length === 0) _clipboard = null;
    await refreshTree();

    if (failures.length === 0) {
      useUIStore.getState().addToast({
        type: 'success',
        message: `Pasted ${successes} item${successes === 1 ? '' : 's'}`,
        duration: 2000,
      });
    } else if (successes === 0) {
      useUIStore.getState().addToast({
        type: 'error',
        message: `Paste failed: ${toError(failures[0].reason).message}`,
        duration: 5000,
      });
    } else {
      useUIStore.getState().addToast({
        type: 'warning',
        message:
          `Pasted ${successes}/${results.length}, ` +
          `${failures.length} failed: ${toError(failures[0].reason).message}`,
        duration: 5000,
      });
    }
  }, [type, path, parentDir, refreshTree, onClose]);

  // ═══════════════════════════════════════════════
  // Input mode (rename / new file / new folder)
  // ═══════════════════════════════════════════════

  // Click-outside handler for inline input mode (rename / new-file / new-folder)
  const inlineRef = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (mode === 'menu') return;
    const handler = (e: MouseEvent) => {
      if (inlineRef.current && !inlineRef.current.contains(e.target as Node)) {
        onClose();
      }
    };
    document.addEventListener('mousedown', handler);
    return () => document.removeEventListener('mousedown', handler);
  }, [mode, onClose]);

  if (mode === 'new-file' || mode === 'new-folder') {
    const label = mode === 'new-folder' ? 'New Folder' : 'New File';
    const onConfirm = handleCreateConfirm;
    return (
      <div
        ref={inlineRef}
        className="ws-context-menu"
        style={{ position: 'fixed', left: state.x, top: state.y }}
      >
        <div style={{ padding: '6px 12px 2px 8px', fontSize: '11px', color: 'var(--text-muted)' }}>{label}</div>
        <div className="ws-inline-input-row">
          <input
            ref={inputRef}
            className="ws-inline-input"
            value={inputValue}
            onChange={(e) => setInputValue(e.target.value)}
            onKeyDown={(e) => {
              if (e.key === 'Enter') { e.preventDefault(); void onConfirm(); }
              if (e.key === 'Escape') { e.preventDefault(); onClose(); }
            }}
            onBlur={() => { setTimeout(() => void onConfirm(), 100); }}
            placeholder={mode === 'new-folder' ? 'Folder name...' : 'File name...'}
            autoFocus
          />
        </div>
      </div>
    );
  }

  // ═══════════════════════════════════════════════
  // Build menu items — exact match to vanilla screenshots
  // ═══════════════════════════════════════════════

  const items: MenuItem[] = [];

  if (type === 'file') {
    // ── File context menu (matches file_right_click.png) ──
    items.push({ label: 'Open', icon: '📄', onClick: handleOpen });
    items.push({ label: 'Open Preview', icon: '👁', onClick: () => void handleOpenPreview() });
    items.push({ label: 'Open With\u2026', icon: '📂', onClick: () => void handleOpenWith() });
    items.push({ label: '', separator: true });

    if (_compareSelection && _compareSelection !== path) {
      items.push({ label: 'Compare with Selected', icon: '↔', onClick: () => void handleCompareWithSelected() });
    } else {
      items.push({ label: 'Select for Compare', icon: '↔', onClick: handleSelectForCompare });
    }
    items.push({ label: 'Find references to this file', icon: '🔍', onClick: () => void handleFindFileReferences() });
    items.push({ label: 'Open Timeline', icon: '⏱', onClick: () => void handleOpenTimeline() });
    items.push({ label: '', separator: true });

    items.push({ label: 'Add File to Chat', icon: '💬', onClick: () => void handleAddFileToChat() });
    items.push({ label: '', separator: true });

    items.push({ label: 'Cut', icon: '✂', shortcut: `${MOD}X`, onClick: handleCut });
    items.push({ label: 'Copy', icon: '📋', shortcut: `${MOD}C`, onClick: handleCopyFile });
    items.push({ label: 'Copy Path', icon: '📎', shortcut: `${OPT}${MOD}C`, onClick: handleCopyPath });
    items.push({ label: 'Copy Relative Path', icon: '📄', shortcut: `${OPT}${SHIFT}${MOD}C`, onClick: handleCopyRelativePath });
    items.push({ label: '', separator: true });

    items.push({ label: 'Rename\u2026', icon: '✏️', shortcut: '\u21B5', onClick: handleRenameStart });
    items.push({ label: 'Duplicate', icon: '📋', onClick: () => void handleDuplicate() });
    items.push({ label: 'Delete', icon: '🗑', danger: true, shortcut: `${MOD}\u232B`, onClick: () => void handleDelete() });
    items.push({ label: '', separator: true });

    items.push({ label: 'Reveal in Finder', icon: '📂', shortcut: `${OPT}${MOD}R`, onClick: handleRevealInFinder });
  } else {
    // ── Folder context menu (matches folder_right_click.png) ──
    items.push({ label: 'New File\u2026', icon: '📄', onClick: handleNewFileStart, closeOnClick: false });
    items.push({ label: 'New Folder\u2026', icon: '📁', onClick: handleNewFolderStart, closeOnClick: false });
    items.push({ label: '', separator: true });

    items.push({ label: 'Find in Folder\u2026', icon: '🔍', shortcut: `${OPT}${SHIFT}F`, onClick: () => void handleFindInFolder() });
    items.push({ label: 'Add Folder to Chat', icon: '💬', onClick: () => void handleAddFolderToChat() });
    items.push({ label: '', separator: true });

    items.push({ label: 'Paste', icon: '📋', shortcut: `${MOD}V`, disabled: !_clipboard, onClick: () => void handlePaste() });
    items.push({ label: '', separator: true });

    items.push({ label: 'Copy Path', icon: '📎', shortcut: `${OPT}${MOD}C`, onClick: handleCopyPath });
    items.push({ label: 'Copy Relative Path', icon: '📄', shortcut: `${OPT}${SHIFT}${MOD}C`, onClick: handleCopyRelativePath });
    items.push({ label: '', separator: true });

    items.push({ label: 'Rename\u2026', icon: '✏️', shortcut: '\u21B5', onClick: handleRenameStart });
    items.push({ label: 'Delete', icon: '🗑', danger: true, onClick: () => void handleDelete() });
    items.push({ label: '', separator: true });

    items.push({ label: 'Reveal in Finder', icon: '📂', shortcut: `${OPT}${MOD}R`, onClick: handleRevealInFinder });
  }

  return (
    <ContextMenu
      items={items}
      position={{ x: state.x, y: state.y }}
      onClose={onClose}
    />
  );
};
