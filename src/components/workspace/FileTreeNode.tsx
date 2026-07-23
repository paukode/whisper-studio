import React, { useCallback, useState } from 'react';
import type { FileTreeEntry } from '@/types/workspace';
import { getFileIcon } from '@/utils/fileIcons';
import { InlineInput } from './InlineInput';

export interface FileTreeNodeProps {
  /** The file or directory entry to render. */
  entry: FileTreeEntry;
  /** Nesting depth for indentation. */
  depth: number;
  /** Path of the currently selected file (for highlight). */
  selectedPath: string | null;
  /** Called on single-click to select/highlight without opening. */
  onSelect: (path: string) => void;
  /** Called on double-click to open the file in the editor. */
  onFileSelect: (path: string) => void;
  /** Called when a directory is expanded for the first time (lazy load). */
  onExpand: (path: string) => Promise<void>;
  /** Called on right-click for context menu. */
  onContextMenu: (event: React.MouseEvent, path: string, type: 'file' | 'directory') => void;
  /** If set, this node is being renamed. */
  renamingPath: string | null;
  /** Called to start renaming a node (e.g. F2 key). */
  onRenameStart?: (path: string) => void;
  /** Called when rename is confirmed. */
  onRenameConfirm: (oldPath: string, newName: string) => void;
  /** Called when rename is cancelled. */
  onRenameCancel: () => void;
}

/**
 * Single tree node with expand/collapse, file/folder icons, and selection highlight.
 */
export const FileTreeNode: React.FC<FileTreeNodeProps> = ({
  entry,
  depth,
  selectedPath,
  onSelect,
  onFileSelect,
  onExpand,
  onContextMenu,
  renamingPath,
  onRenameStart,
  onRenameConfirm,
  onRenameCancel,
}) => {
  const [expanded, setExpanded] = useState(false);
  const [loading, setLoading] = useState(false);

  const isDirectory = entry.type === 'directory';
  const isSelected = entry.path === selectedPath;
  const isRenaming = entry.path === renamingPath;

  /** Single click: select file, expand/collapse directory. */
  const handleClick = useCallback(async () => {
    if (isDirectory) {
      if (!expanded && (!entry.children || entry.children.length === 0)) {
        setLoading(true);
        try {
          await onExpand(entry.path);
        } finally {
          setLoading(false);
        }
      }
      setExpanded((prev) => !prev);
    }
    onSelect(entry.path);
  }, [isDirectory, expanded, entry.path, entry.children, onExpand, onSelect]);

  /** Double click: open file in editor. */
  const handleDoubleClick = useCallback(() => {
    if (!isDirectory) {
      onFileSelect(entry.path);
    }
  }, [isDirectory, entry.path, onFileSelect]);

  const handleContextMenu = useCallback(
    (e: React.MouseEvent) => {
      e.preventDefault();
      onContextMenu(e, entry.path, entry.type);
    },
    [onContextMenu, entry.path, entry.type],
  );

  const handleRenameConfirm = useCallback(
    (newName: string) => {
      onRenameConfirm(entry.path, newName);
    },
    [onRenameConfirm, entry.path],
  );

  const icon = isDirectory ? (expanded ? '📂' : '📁') : getFileIcon(entry.path);

  return (
    <div className="file-tree-node-container" role="treeitem" aria-expanded={isDirectory ? expanded : undefined}>
      <div
        className={`file-tree-node ${isSelected ? 'file-tree-node-selected' : ''}`}
        style={{ paddingLeft: `${depth * 16 + 4}px` }}
        onClick={handleClick}
        onDoubleClick={handleDoubleClick}
        onContextMenu={handleContextMenu}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => {
          if (e.key === 'Enter') {
            e.preventDefault();
            if (isDirectory) {
              void handleClick(); // expand/collapse
            } else {
              // Mac Finder behavior: Enter renames selected file
              onRenameStart?.(entry.path);
            }
          }
          if (e.key === ' ') {
            e.preventDefault();
            void handleClick();
          }
          if (e.key === 'F2') {
            e.preventDefault();
            onRenameStart?.(entry.path);
          }
        }}
        aria-label={`${isDirectory ? 'Folder' : 'File'}: ${entry.name}`}
      >
        {isDirectory && (
          <span className={`file-tree-chevron ${expanded ? 'file-tree-chevron-expanded' : ''}`}>
            {loading ? '⏳' : '▶'}
          </span>
        )}
        <span className="file-tree-icon">{icon}</span>
        {isRenaming ? (
          <InlineInput
            initialValue={entry.name}
            onConfirm={handleRenameConfirm}
            onCancel={onRenameCancel}
          />
        ) : (
          <span className="file-tree-name">{entry.name}</span>
        )}
      </div>

      {isDirectory && expanded && entry.children && (
        <div className="file-tree-children" role="group">
          {entry.children.map((child) => (
            <FileTreeNode
              key={child.path}
              entry={child}
              depth={depth + 1}
              selectedPath={selectedPath}
              onSelect={onSelect}
              onFileSelect={onFileSelect}
              onExpand={onExpand}
              onContextMenu={onContextMenu}
              renamingPath={renamingPath}
              onRenameStart={onRenameStart}
              onRenameConfirm={onRenameConfirm}
              onRenameCancel={onRenameCancel}
            />
          ))}
        </div>
      )}
    </div>
  );
};
