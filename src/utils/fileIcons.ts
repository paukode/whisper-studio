/**
 * Map file extensions to icon emoji. Matches the vanilla JS
 * `_fileIcons` / `_getFileIcon` from ide-helpers.js.
 */

const FILE_ICONS: Record<string, string> = {
  '.js': '🟨', '.jsx': '🟨', '.mjs': '🟨', '.cjs': '🟨',
  '.ts': '🔵', '.tsx': '🔵',
  '.py': '🐍', '.pyw': '🐍',
  '.rb': '💎',
  '.go': '💠',
  '.rs': '⚙️',
  '.java': '☕',
  '.cs': '🟣',
  '.cpp': '🟦', '.cc': '🟦', '.cxx': '🟦', '.c': '🟦', '.h': '🟦', '.hpp': '🟦',
  '.html': '🌐', '.htm': '🌐',
  '.css': '🎨', '.scss': '🎨', '.less': '🎨',
  '.json': '📋', '.jsonc': '📋',
  '.yaml': '📋', '.yml': '📋', '.toml': '📋',
  '.xml': '📋',
  '.svg': '🖼️',
  '.md': '📝', '.mdx': '📝',
  '.txt': '📄',
  '.sh': '📟', '.bash': '📟', '.zsh': '📟',
  '.sql': '🗃️',
  '.png': '🖼️', '.jpg': '🖼️', '.jpeg': '🖼️',
  '.gif': '🖼️', '.webp': '🖼️', '.bmp': '🖼️',
  '.pdf': '📕',
  '.doc': '📘', '.docx': '📘',
  '.xls': '📊', '.xlsx': '📊', '.csv': '📊', '.tsv': '📊',
  '.zip': '📦', '.tar': '📦', '.gz': '📦',
  '.ipynb': '📓',
  '.env': '🔒', '.lock': '🔒',
  '.dockerfile': '🐳',
  '.gitignore': '🚫',
  '.swift': '🦅',
  '.kt': '🟠', '.kts': '🟠',
  '.r': '📈', '.R': '📈',
  '.lua': '🌙',
  '.pl': '🐪', '.pm': '🐪',
  '.dart': '🎯',
  '.vue': '💚',
  '.svelte': '🔥',
  '.ps1': '💻',
  '.bat': '💻', '.cmd': '💻',
  '.graphql': '◆', '.gql': '◆',
  '.tf': '🏗️',
};

/**
 * Get an icon emoji for a file path based on its extension or
 * special filename.
 */
export function getFileIcon(path: string): string {
  const name = path.split('/').pop()?.toLowerCase() ?? '';

  // Special filenames
  if (name === 'dockerfile' || name.startsWith('dockerfile.')) return '🐳';
  if (name === '.gitignore') return '🚫';
  if (name === '.env' || name.startsWith('.env.')) return '🔒';

  const dotIdx = name.lastIndexOf('.');
  if (dotIdx === -1) return '📄';
  const ext = name.slice(dotIdx);
  return FILE_ICONS[ext] ?? '📄';
}

export { FILE_ICONS };
