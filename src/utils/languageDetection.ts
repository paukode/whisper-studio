/**
 * File extension → Monaco language ID mapping.
 * Matches the vanilla JS `_extToLang` / `_getLangForPath` from ide.js.
 */

const EXT_TO_LANG: Record<string, string> = {
  '.js': 'javascript', '.jsx': 'javascript', '.mjs': 'javascript', '.cjs': 'javascript',
  '.ts': 'typescript', '.tsx': 'typescript',
  '.py': 'python', '.pyw': 'python',
  '.json': 'json', '.jsonc': 'json',
  '.html': 'html', '.htm': 'html',
  '.css': 'css', '.scss': 'scss', '.less': 'less',
  '.xml': 'xml', '.svg': 'xml', '.xsl': 'xml',
  '.yaml': 'yaml', '.yml': 'yaml',
  '.md': 'markdown', '.mdx': 'markdown',
  '.sh': 'shell', '.bash': 'shell', '.zsh': 'shell',
  '.sql': 'sql',
  '.java': 'java',
  '.c': 'c', '.h': 'c',
  '.cpp': 'cpp', '.cc': 'cpp', '.cxx': 'cpp', '.hpp': 'cpp',
  '.cs': 'csharp',
  '.go': 'go',
  '.rs': 'rust',
  '.rb': 'ruby',
  '.php': 'php',
  '.swift': 'swift',
  '.kt': 'kotlin', '.kts': 'kotlin',
  '.r': 'r', '.R': 'r',
  '.lua': 'lua',
  '.pl': 'perl', '.pm': 'perl',
  '.dockerfile': 'dockerfile',
  '.toml': 'ini', '.ini': 'ini', '.cfg': 'ini',
  '.graphql': 'graphql', '.gql': 'graphql',
  '.tf': 'hcl',
  '.ps1': 'powershell',
  '.bat': 'bat', '.cmd': 'bat',
  '.m': 'objective-c',
  '.dart': 'dart',
  '.vue': 'html',
  '.svelte': 'html',
};

/**
 * Detect the Monaco language ID for a given file path.
 */
export function getLangForPath(path: string): string {
  const name = (path.split('/').pop() ?? '').toLowerCase();

  // Special filenames
  if (name === 'dockerfile' || name.startsWith('dockerfile.')) return 'dockerfile';
  if (name === 'makefile' || name === 'gnumakefile') return 'plaintext';
  if (name === '.env' || name.startsWith('.env.')) return 'ini';

  const dotIdx = name.lastIndexOf('.');
  if (dotIdx === -1) return 'plaintext';
  const ext = name.slice(dotIdx);
  return EXT_TO_LANG[ext] ?? 'plaintext';
}
