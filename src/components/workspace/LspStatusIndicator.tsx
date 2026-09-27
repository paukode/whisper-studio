import React from 'react';
import type { LspStatus } from '@/hooks/useLsp';
import { useUIStore } from '@/stores/uiStore';

/** Human-readable label for the LSP status dot's tooltip + text. */
export function lspStatusLabel(status: LspStatus, language: string, failure: string | null): string {
  const server = `${language} language server`;
  switch (status) {
    case 'connecting':
      return `Connecting to ${server}…`;
    case 'connected':
      return `${server}: connected`;
    case 'error':
      return failure ? `${server}: unavailable. ${failure}` : `${server}: unavailable`;
    default:
      return `${server}: off`;
  }
}

const PILL_STYLE: React.CSSProperties = { position: 'absolute', top: 4, right: 14, zIndex: 4 };

/**
 * Small language-server status pill rendered in the editor's top-right chrome.
 * Uses the shared `.ws-lsp-status` / `.ws-lsp-dot` classes from
 * static/modules/lsp-client.css so the dot color tracks the connection state.
 *
 * When the backend said why the server is not running, the reason is the
 * pill's tooltip and accessible name, and the pill opens Settings > Code tools,
 * which shows every code tool's state and how to fix it.
 */
export const LspStatusIndicator: React.FC<{
  status: LspStatus;
  language: string;
  failure: string | null;
}> = ({ status, language, failure }) => {
  const openSettings = useUIStore((s) => s.openSettings);
  const dotClass = status === 'closed' ? '' : ` ${status}`;
  const label = lspStatusLabel(status, language, failure);
  if (status === 'error' && failure) {
    return (
      <button
        type="button"
        className="ws-lsp-status"
        title={`${label}\nOpen Settings > Code tools`}
        aria-label={`${label} Open Settings, Code tools.`}
        onClick={() => openSettings('code-tools')}
        style={PILL_STYLE}
      >
        <span className={`ws-lsp-dot${dotClass}`} />
        <span>LSP</span>
      </button>
    );
  }
  return (
    <div className="ws-lsp-status" title={label} role="status" aria-label={label} style={PILL_STYLE}>
      <span className={`ws-lsp-dot${dotClass}`} />
      <span>LSP</span>
    </div>
  );
};
