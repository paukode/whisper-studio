import React, { useCallback, useEffect, useState } from 'react';
import { useUIStore, type ExplorerTarget } from '@/stores/uiStore';
import { useDockStore } from '@/stores/dockStore';
import { post } from '@/api/client';
import type { ExploreFactCite } from '@/api/workspace';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import { BrowsePane } from './BrowsePane';
import { OverviewPage } from './OverviewPage';
import { EntityPage } from './EntityPage';
import { FilePage } from './FilePage';

/**
 * The index explorer — the words-first replacement for the relationship-graph
 * overlay. One dialog, three pages (overview, entity, file) plus a browse pane
 * of every extracted name; every element on screen is a word, a sentence, or a
 * count next to its own label. Opened from the workspace panel's Explore
 * button, a file's "Show connections" context item, the command palette, and
 * the connect dialog's indexed-folder menu — all deep-linking via
 * uiStore.openIndexExplorer.
 */
export const IndexExplorer: React.FC = () => {
  const state = useUIStore((s) => s.indexExplorer);
  if (!state) return null;
  // Keyed by the open-call sequence so every openIndexExplorer call remounts
  // the dialog, re-anchoring its navigation stack on the requested page
  // without any reset-state-in-effect.
  return <ExplorerDialog key={state.seq} workspace={state.path} initial={state.target} />;
};

const ExplorerDialog: React.FC<{ workspace: string; initial: ExplorerTarget }> = ({ workspace, initial }) => {
  const close = useUIStore((s) => s.closeIndexExplorer);

  const [stack, setStack] = useState<ExplorerTarget[]>([initial]);
  const [query, setQuery] = useState('');
  const [coachOpen, setCoachOpen] = useState(
    () => localStorage.getItem(STORAGE_KEYS.EXPLORER_COACH_DISMISSED) !== '1',
  );

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') { e.preventDefault(); close(); }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [close]);

  const go = useCallback((target: ExplorerTarget) => {
    setStack((s) => [...s, target]);
  }, []);
  const back = useCallback(() => setStack((s) => (s.length > 1 ? s.slice(0, -1) : s)), []);

  const absPath = useCallback(
    (rel: string) => (rel.startsWith('/') ? rel : `${workspace.replace(/\/+$/, '')}/${rel}`),
    [workspace],
  );

  // Citations and Open land in the dock's file viewer, which sits under this
  // full-screen overlay — close the explorer so the opened file is visible.
  const openFile = useCallback(
    (rel: string, startLine?: number, endLine?: number) => {
      useDockStore.getState().openFile({
        path: absPath(rel),
        title: rel.split('/').pop() || rel,
        startLine,
        endLine,
      });
      close();
    },
    [absPath, close],
  );
  const openCite = useCallback(
    (cite: ExploreFactCite) => openFile(cite.path, cite.start_line ?? undefined, cite.end_line ?? undefined),
    [openFile],
  );
  const revealFile = useCallback(
    (rel: string) => {
      void post('/api/workspace/reveal', { path: absPath(rel) }).catch(() => {
        useUIStore.getState().addToast({ type: 'error', message: 'Could not reveal the file.' });
      });
    },
    [absPath],
  );

  const dismissCoach = useCallback(() => {
    localStorage.setItem(STORAGE_KEYS.EXPLORER_COACH_DISMISSED, '1');
    setCoachOpen(false);
  }, []);

  const current = stack[stack.length - 1];
  const selectedEntity = current.kind === 'entity' ? { name: current.name } : null;
  const folder = workspace.split('/').filter(Boolean).pop() || workspace;

  return (
    <div className="xpl-overlay" onClick={(e) => { if (e.target === e.currentTarget) close(); }}>
      <div className="xpl-dialog" role="dialog" aria-modal="true" aria-label={`Explore index: ${folder}`}>
        <div className="xpl-header">
          {stack.length > 1 ? (
            <button type="button" className="xpl-back" onClick={back} aria-label="Back">&#8592;</button>
          ) : (
            <span className="xpl-back-placeholder" />
          )}
          <div className="xpl-title">
            Explore index<span className="xpl-title-ws">{folder}</span>
          </div>
          {current.kind !== 'overview' && (
            <button type="button" className="xpl-home" onClick={() => setStack([{ kind: 'overview' }])}>
              Overview
            </button>
          )}
          <span className="xpl-spacer" />
          <input
            type="text"
            className="xpl-search"
            placeholder="Find a person, organization, or topic…"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
          />
          <button
            type="button"
            className="xpl-help"
            title="What am I looking at?"
            aria-label="What am I looking at?"
            onClick={() => setCoachOpen(true)}
          >
            ?
          </button>
          <button type="button" className="xpl-close" onClick={close} aria-label="Close">&#xD7;</button>
        </div>

        {coachOpen && (
          <div className="xpl-coach">
            <div>
              These are the people, organizations, and topics found in this folder's files.
              Click a name to see everything about it, or a file to see what it connects to and why.
              Every count says exactly what it measures.
            </div>
            <button type="button" className="btn btn-sm" onClick={dismissCoach}>Got it</button>
          </div>
        )}

        <div className="xpl-body">
          <BrowsePane workspace={workspace} query={query} selected={selectedEntity} go={go} />
          <div className="xpl-main">
            {current.kind === 'overview' && <OverviewPage workspace={workspace} go={go} />}
            {current.kind === 'entity' && (
              <EntityPage workspace={workspace} name={current.name} label={current.label} go={go} openCite={openCite} />
            )}
            {current.kind === 'file' && (
              <FilePage workspace={workspace} file={current.file} go={go} openFile={openFile} revealFile={revealFile} />
            )}
          </div>
        </div>
      </div>
    </div>
  );
};
