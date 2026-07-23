import React, { useCallback, useMemo } from 'react';
import { useSettingsStore } from '@/stores/settingsStore';
import { useUIStore } from '@/stores/uiStore';
import { useSessionStore } from '@/stores/sessionStore';
import { useMcpToggle } from '@/hooks/useMcpToggle';
import { post } from '@/api/client';
import type { IndexInfo } from '@/api/workspace';

export type MoreSection = 'skills' | 'index' | 'mcp' | null;

interface MoreMenuProps {
  open: boolean;
  section: MoreSection;
  setSection: React.Dispatch<React.SetStateAction<MoreSection>>;
  /** Toggle the menu; the parent closes its sibling dropdowns when opening. */
  onToggle: () => void;
  /** Close the menu (used by the action rows that navigate away). */
  onClose: () => void;
  indexes: IndexInfo[];
  selectedIndexes: string[];
  toggleIndex: (path: string) => void;
  wsConnected: boolean;
  /** Prepend "@skill " to the composer and refocus it. */
  onInsertSkill: (name: string) => void;
}

/** "meeting_notes" / "code-review" → "Meeting Notes" / "Code Review" for the
 *  skills list; the raw @handle is shown alongside for the mention. */
function prettySkillName(name: string): string {
  return name
    .replace(/[_-]+/g, ' ')
    .replace(/\b\w/g, (c) => c.toUpperCase());
}

/**
 * "+ More" overflow popover holding the less-frequent toolbar controls (brief,
 * auto memory, skills, index search, MCP servers, project memory) so the chat
 * toolbar keeps to its four primary controls. The dot lights up when a hidden
 * toggle is active — including a narrowed index selection — so nothing important
 * is silently buried. Extracted from ChatInput to keep that file under budget;
 * it self-subscribes to the same stores rather than threading ~10 props.
 */
export const MoreMenu: React.FC<MoreMenuProps> = ({
  open, section, setSection, onToggle, onClose,
  indexes, selectedIndexes, toggleIndex, wsConnected, onInsertSkill,
}) => {
  const autoMemory = useSettingsStore((s) => s.autoMemory);
  const setAutoMemory = useSettingsStore((s) => s.setAutoMemory);
  const skills = useSettingsStore((s) => s.skills);
  const mcpServers = useSettingsStore((s) => s.mcpServers);
  const openSettings = useUIStore((s) => s.openSettings);
  const openMemoryEditor = useUIStore((s) => s.openMemoryEditor);
  const openMemoryViewer = useUIStore((s) => s.openMemoryViewer);
  const addToast = useUIStore((s) => s.addToast);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const toggleMcpServer = useMcpToggle();

  // Active = servers whose persisted `enabled` flag is on (shared with Settings).
  const mcpActiveSet = useMemo<Set<string>>(
    () => new Set(mcpServers.filter((s) => s.enabled).map((s) => s.name)),
    [mcpServers],
  );
  const mcpActiveCount = mcpActiveSet.size;

  const resetStuckSession = useCallback(async () => {
    onClose();
    if (!currentSessionId) return;
    try {
      await post(`/api/chat/sessions/${encodeURIComponent(currentSessionId)}/reset`, {});
      addToast({ type: 'success', message: 'Session reset. You can send a new message now.', duration: 4000 });
    } catch {
      addToast({ type: 'error', message: 'Could not reset the session.', duration: 4000 });
    }
  }, [currentSessionId, addToast, onClose]);

  return (
    <div className="toolbar-dropdown-wrap">
      <button
        type="button"
        className={`toolbar-btn more-btn${open ? ' active' : ''}`}
        id="moreBtn"
        title="More controls"
        aria-label="More controls"
        aria-expanded={open}
        aria-controls="moreMenu"
        onClick={onToggle}
      >
        <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
          <line x1="12" y1="5" x2="12" y2="19" /><line x1="5" y1="12" x2="19" y2="12" />
        </svg>
        More
        {(autoMemory || mcpActiveCount > 0
          || (indexes.length > 0 && selectedIndexes.length < indexes.length)) && (
          <span className="more-dot" aria-hidden="true" />
        )}
      </button>
      <div className="toolbar-dropdown more-pop" id="moreMenu" role="group" aria-label="More controls" style={{ display: open ? 'block' : 'none' }}>
        {/* Response length now lives in the toolbar (Brief/Normal/Detailed),
         *  so it's no longer duplicated here. */}
        <button
          type="button"
          className={`more-row${autoMemory ? ' on' : ''}`}
          onClick={() => setAutoMemory(!autoMemory)}
          title={`Auto memory, two tiers.\nGlobal: cross-project facts (preferences, feedback), works in every chat, no workspace needed.\nProject: workspace-scoped facts, active when a workspace is open.\nWhen on, the assistant records and recalls memories automatically.`}
        >
          <span className="more-row-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M12 2a7 7 0 0 1 7 7c0 3-2 5.5-4 7l-1 2H10l-1-2c-2-1.5-4-4-4-7a7 7 0 0 1 7-7z" /><line x1="10" y1="22" x2="14" y2="22" />
            </svg>
            Memory
          </span>
          <span className="more-row-state">{autoMemory ? 'On' : 'Off'}</span>
        </button>

        {/* Browse/edit the auto-memory store (global + project tiers). */}
        <button
          type="button"
          className="more-row"
          onClick={() => { onClose(); openMemoryViewer(); }}
          title={'Browse what the assistant remembers: global and project memory files.\nView, edit, delete, or promote project memories to global.'}
        >
          <span className="more-row-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M4 19.5A2.5 2.5 0 0 1 6.5 17H20" /><path d="M6.5 2H20v20H6.5A2.5 2.5 0 0 1 4 19.5v-15A2.5 2.5 0 0 1 6.5 2z" />
            </svg>
            Memory files
          </span>
          <svg className="more-row-go" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>

        <div className="toolbar-dropdown-header">Tools</div>

        {/* Skills section */}
        <button
          type="button"
          className={`more-row${section === 'skills' ? ' expanded' : ''}`}
          onClick={() => setSection((s) => (s === 'skills' ? null : 'skills'))}
          aria-expanded={section === 'skills'}
          aria-controls="more-sec-skills"
        >
          <span className="more-row-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <polygon points="13 2 3 14 12 14 11 22 21 10 12 10 13 2" />
            </svg>
            Skills
          </span>
          <svg className="more-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>
        {section === 'skills' && (
          <div className="more-section" id="more-sec-skills" role="group" aria-label="Skills">
            <div
              className="toolbar-dropdown-item toolbar-dropdown-manage"
              onClick={() => { onClose(); openSettings('skills'); }}
            >
              <span className="toolbar-dropdown-item-name">Manage Skills...</span>
            </div>
            {skills.length === 0 ? (
              <div className="toolbar-dropdown-empty">No skills loaded</div>
            ) : (
              [...skills].sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' })).map((s) => (
                <div key={s.name} className="toolbar-dropdown-item" onClick={() => onInsertSkill(s.name)}
                  style={{ flexDirection: 'row', alignItems: 'flex-start', justifyContent: 'space-between' }}>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '1px', flex: 1, minWidth: 0 }}>
                    <span className="toolbar-dropdown-item-name">
                      {prettySkillName(s.name)}
                      <span className="skill-handle">@{s.name}</span>
                    </span>
                    {s.description && <span className="toolbar-dropdown-item-desc">{s.description}</span>}
                  </div>
                  {s.enabled && (
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="var(--accent)" strokeWidth="2.5" strokeLinecap="round" strokeLinejoin="round" style={{ flexShrink: 0, marginLeft: '8px', marginTop: '2px' }}>
                      <polyline points="20 6 9 17 4 12" />
                    </svg>
                  )}
                </div>
              ))
            )}
          </div>
        )}

        {/* Index search section — only when something is indexed */}
        {indexes.length > 0 && (
          <>
            <button
              type="button"
              className={`more-row${selectedIndexes.length > 0 ? ' on' : ''}${section === 'index' ? ' expanded' : ''}`}
              onClick={() => setSection((s) => (s === 'index' ? null : 'index'))}
              aria-expanded={section === 'index'}
              aria-controls="more-sec-index"
              title={`Search indexes (${selectedIndexes.length}/${indexes.length})`}
            >
              <span className="more-row-label">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                  <circle cx="11" cy="11" r="7" /><path d="M21 21l-4.3-4.3" />
                </svg>
                Index search
              </span>
              <span className="more-row-state">{selectedIndexes.length}/{indexes.length}</span>
              <svg className="more-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
                <polyline points="9 18 15 12 9 6" />
              </svg>
            </button>
            {section === 'index' && (
              <div className="more-section" id="more-sec-index" role="group" aria-label="Index search">
                <div className="toolbar-dropdown-header">Search these indexes</div>
                {indexes.map((ix) => {
                  const on = selectedIndexes.includes(ix.path);
                  return (
                    <div
                      key={ix.path}
                      className="toolbar-dropdown-item"
                      style={{ flexDirection: 'row', alignItems: 'center', gap: 8, cursor: 'pointer' }}
                      onClick={(e) => { e.stopPropagation(); toggleIndex(ix.path); }}
                    >
                      <input
                        type="checkbox"
                        checked={on}
                        onChange={() => toggleIndex(ix.path)}
                        onClick={(e) => e.stopPropagation()}
                        style={{ flexShrink: 0 }}
                      />
                      <div style={{ display: 'flex', flexDirection: 'column', gap: '1px', minWidth: 0 }}>
                        <span className="toolbar-dropdown-item-name">{ix.name}</span>
                        <span className="toolbar-dropdown-item-desc">{ix.files} files · {ix.chunks} chunks</span>
                      </div>
                    </div>
                  );
                })}
              </div>
            )}
          </>
        )}

        {/* MCP section */}
        <button
          type="button"
          className={`more-row${mcpActiveCount > 0 ? ' on' : ''}${section === 'mcp' ? ' expanded' : ''}`}
          onClick={() => setSection((s) => (s === 'mcp' ? null : 'mcp'))}
          aria-expanded={section === 'mcp'}
          aria-controls="more-sec-mcp"
          title={`MCP servers enabled (${mcpActiveCount}/${mcpServers.length})`}
        >
          <span className="more-row-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <circle cx="12" cy="12" r="3" /><path d="M12 1v4M12 19v4M4.2 4.2l2.8 2.8M17 17l2.8 2.8M1 12h4M19 12h4M4.2 19.8l2.8-2.8M17 7l2.8-2.8" />
            </svg>
            MCP servers
          </span>
          <span className="more-row-state">{mcpActiveCount}/{mcpServers.length}</span>
          <svg className="more-chev" width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polyline points="9 18 15 12 9 6" />
          </svg>
        </button>
        {section === 'mcp' && (
          <div className="more-section" id="more-sec-mcp" role="group" aria-label="MCP servers">
            <div
              className="toolbar-dropdown-item toolbar-dropdown-manage"
              onClick={() => { onClose(); openSettings('mcp'); }}
            >
              <span className="toolbar-dropdown-item-name">Manage MCP Servers...</span>
            </div>
            {mcpServers.length === 0 ? (
              <div className="toolbar-dropdown-empty">No MCP servers configured</div>
            ) : (
              [...mcpServers]
                .sort((a, b) => a.name.localeCompare(b.name, undefined, { sensitivity: 'base' }))
                .map((s) => {
                  const active = mcpActiveSet.has(s.name);
                  return (
                    <div
                      key={s.name}
                      className="toolbar-dropdown-item"
                      onClick={(e) => {
                        // Stop the row's click from also closing the menu;
                        // we want the user to flip multiple servers in one open.
                        e.stopPropagation();
                        void toggleMcpServer(s.name, !active);
                      }}
                      style={{ flexDirection: 'row', alignItems: 'center', justifyContent: 'space-between', cursor: 'pointer' }}
                    >
                      <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0 }}>
                        <input
                          type="checkbox"
                          checked={active}
                          onChange={() => void toggleMcpServer(s.name, !active)}
                          onClick={(e) => e.stopPropagation()}
                          style={{ flexShrink: 0 }}
                        />
                        <div style={{ display: 'flex', flexDirection: 'column', gap: '1px', minWidth: 0 }}>
                          <span className="toolbar-dropdown-item-name">{s.name}</span>
                          <span className="toolbar-dropdown-item-desc">{s.status}</span>
                        </div>
                      </div>
                      <span style={{
                        width: '8px', height: '8px', borderRadius: '50%', flexShrink: 0, marginLeft: '8px',
                        background: s.status === 'running' || s.status === 'connected' ? 'var(--success)' : s.status === 'error' ? 'var(--danger)' : 'var(--text-muted)',
                      }} />
                    </div>
                  );
                })
            )}
          </div>
        )}

        {/* Project memory (WHISPER.md) — workspace-scoped editor. Distinct from
         *  the auto-memory toggle above (which covers the global + project
         *  memory stores); this edits the hand-written per-repo instructions. */}
        {wsConnected && (
          <>
            <div className="more-divider" />
            <button
              type="button"
              className="more-row"
              onClick={() => { onClose(); openMemoryEditor(); }}
              title={`Project memory: WHISPER.md in this workspace only.\nThe assistant reads this on every turn for project-specific context.`}
            >
              <span className="more-row-label">
                <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
                  <ellipse cx="12" cy="5" rx="9" ry="3" /><path d="M21 12c0 1.66-4 3-9 3s-9-1.34-9-3" /><path d="M3 5v14c0 1.66 4 3 9 3s9-1.34 9-3V5" />
                </svg>
                Project memory
              </span>
              <svg className="more-row-go" width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" aria-hidden="true">
                <path d="M12 20h9" /><path d="M16.5 3.5a2.12 2.12 0 0 1 3 3L7 19l-4 1 1-4z" />
              </svg>
            </button>
          </>
        )}

        {/* Reset a wedged session: clears the server-side in-flight-stream slot
         *  and paused state so a "session is busy" 409 recovers without an app
         *  restart. Always shown; harmless when nothing is stuck. */}
        <div className="more-divider" />
        <button
          type="button"
          className="more-row"
          onClick={resetStuckSession}
          title={"Reset session\nClears a stuck 'session is busy' state (e.g. after the app was suspended) without restarting the app. Does not delete any messages."}
        >
          <span className="more-row-label">
            <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M3 12a9 9 0 1 0 3-6.7L3 8" /><path d="M3 3v5h5" />
            </svg>
            Reset session
          </span>
        </button>
      </div>
    </div>
  );
};
