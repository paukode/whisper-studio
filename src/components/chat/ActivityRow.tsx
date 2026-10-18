import React, { useEffect, useMemo, useState } from 'react';
import type { MediaSize, ToolUseEvent } from '@/types/chat';
import { ICON, iconFor } from '@/components/chat/activityIcons';
import { basename, callVerb, describeCall, describeGroup, isRunning, type CallDesc, type GroupState } from '@/components/chat/activityLabel';
import { callOutcome, formatCount, groupChips, type Chip } from '@/components/chat/activityChips';
import { PreviewScreenshotCard } from '@/components/chat/PreviewScreenshotCard';
import { useUIStore } from '@/stores/uiStore';
import { useDockStore } from '@/stores/dockStore';

/**
 * ActivityRow: a group of consecutive tool calls inside an assistant bubble,
 * drawn as one line that reads as a sentence ("Read 3 files", "Edited
 * ActivityRow.tsx, ran 2 commands") with the files or the command as a mono
 * tail and the numbers as chips on the right. The same component draws a
 * group of one call and a group of many.
 *
 * Clicking the line opens one detail line per call: verb, subject (a path
 * links to the editor, a diff pill opens the diff), outcome, and a toggle
 * for that call's raw output. Everything stays inside the chat column.
 *
 * UX rules:
 *   - Collapsed by default. A group with an error opens (never hide a
 *     failure behind a chevron), and so does a goal the judge could not
 *     check, so its reason is in view, and a preview screenshot.
 *   - Every word comes from activityLabel.ts and every number from
 *     activityChips.ts; nothing re-runs a tool or counts a cut preview.
 */

export interface ActivityRowProps {
  tools: ToolUseEvent[];
  /** The model-written caption for this group (ChatMessage.activityLabels,
   *  keyed by the group's first toolId). Until it lands the deterministic
   *  sentence is the label; after, the sentence is the tail. */
  writtenLabel?: string;
  /** A preview screenshot's saved size and setter, so a committed message
   *  keeps the size the user dragged it to. Without it the shot shows at its
   *  natural size with no grip. */
  mediaFor?: (tool: ToolUseEvent) => { size?: MediaSize; onResize?: (size: MediaSize | null) => void };
}

/** The path a detail line shows (workspace-relative when an absolute input
 *  lies inside the workspace) and the absolute path the editor opens: an
 *  absolute input as is, a relative one joined to the workspace, null when
 *  there is no workspace to resolve it against. */
export function resolvePath(path: string, wsPath: string): { shown: string; absolute: string | null } {
  const root = wsPath.replace(/\/+$/, '');
  if (path.startsWith('/')) {
    const shown = root && path.startsWith(`${root}/`) ? path.slice(root.length + 1) : path;
    return { shown, absolute: path };
  }
  const rel = path.replace(/^\.\//, '');
  return { shown: rel, absolute: root && !rel.startsWith('~') ? `${root}/${rel}` : null };
}

/** The diff stats of an edit or a group: +added -removed. A button that
 *  opens the diff when it belongs to one call, a plain chip on a group. */
export const DiffPill: React.FC<{ added: number; removed: number; onOpen?: () => void }> = ({ added, removed, onOpen }) => {
  const body = (
    <>
      <span className="add">+{formatCount(added)}</span>
      <span className="del">-{formatCount(removed)}</span>
    </>
  );
  if (!onOpen) return <span className="activity-pill">{body}</span>;
  return (
    <button
      type="button"
      className="activity-pill"
      title="Open the diff"
      onClick={(e) => {
        e.stopPropagation();
        onOpen();
      }}
    >
      {body}
    </button>
  );
};

function ChipView({ chip }: { chip: Chip }) {
  if (chip.kind === 'diff') return <DiffPill added={chip.added} removed={chip.removed} />;
  return <span className={`activity-chip${chip.tone === 'neutral' ? '' : ` ${chip.tone}`}`}>{chip.text}</span>;
}

const Spinner = () => <span className="activity-spinner" role="img" aria-label="Running" />;

/** The group's icon slot: its kind's icon when done, a ring while running,
 *  the alert on an error, the stop square when stopped. */
function stateMark(state: GroupState, tools: ToolUseEvent[]): React.ReactNode {
  if (state === 'running') return <Spinner />;
  if (state === 'error') return ICON.alertCircle;
  if (state === 'stopped') return ICON.stop;
  return iconFor(tools[0]?.toolName ?? '');
}

/** Seconds since the group started running, counted on the client clock;
 *  null once it is not running. The epoch is bumped each time the group
 *  starts running again, so a later run counts from zero. */
function useElapsedSeconds(running: boolean): number | null {
  const [run, setRun] = useState({ running, epoch: 0 });
  if (run.running !== running) setRun({ running, epoch: running ? run.epoch + 1 : run.epoch });
  const [tick, setTick] = useState({ epoch: -1, seconds: 0 });
  const { epoch } = run;
  useEffect(() => {
    if (!running) return;
    const start = Date.now();
    const id = setInterval(() => {
      setTick({ epoch, seconds: Math.floor((Date.now() - start) / 1000) });
    }, 1000);
    return () => clearInterval(id);
  }, [running, epoch]);
  if (!running) return null;
  return tick.epoch === epoch ? tick.seconds : 0;
}

interface CallLineProps {
  tool: ToolUseEvent;
  expanded: boolean;
  onToggle: () => void;
  wsPath: string;
  mediaFor?: ActivityRowProps['mediaFor'];
}

function CallSubject({ desc, wsPath }: { desc: CallDesc; wsPath: string }) {
  if (desc.kind === 'card') {
    return (
      <span className="activity-call-subject prose wide" title={desc.line}>
        <span className="activity-call-text">{desc.line}</span>
      </span>
    );
  }
  if (desc.path && (desc.kind === 'file' || desc.kind === 'list')) {
    const { shown, absolute } = resolvePath(desc.path, wsPath);
    const cut = shown.lastIndexOf('/', shown.length - 2);
    const dir = cut >= 0 ? shown.slice(0, cut + 1) : '';
    const base = cut >= 0 ? shown.slice(cut + 1) : shown;
    const parts = (
      <>
        {dir && <span className="activity-call-dir">{dir}</span>}
        <span className="activity-call-base">{base}</span>
      </>
    );
    // A file that is gone, or a folder, has nothing for the editor to open.
    const linked = absolute !== null && desc.kind === 'file' && desc.verb !== 'deleted';
    if (!linked) return <span className="activity-call-subject" title={shown}>{parts}</span>;
    return (
      <button
        type="button"
        className="activity-call-subject activity-call-link"
        title="Open in the editor"
        onClick={(e) => {
          e.stopPropagation();
          useDockStore.getState().openFile({ path: absolute, title: basename(absolute) });
        }}
      >
        {parts}
      </button>
    );
  }
  if (desc.kind === 'search') {
    const scope = desc.scopePath ? resolvePath(desc.scopePath, wsPath).shown : null;
    return (
      <span className="activity-call-subject" title={[desc.subject, scope].filter(Boolean).join(' in ')}>
        <span className="activity-call-text">{desc.subject ?? ''}</span>
        {scope && <span className="activity-call-dim">{` in ${scope}`}</span>}
      </span>
    );
  }
  const line = desc.kind === 'command' ? desc.line : desc.line || desc.subject || '';
  return (
    <span className="activity-call-subject" title={line || undefined}>
      <span className="activity-call-text">{line}</span>
    </span>
  );
}

function CallLine({ tool, expanded, onToggle, wsPath, mediaFor }: CallLineProps) {
  const desc = describeCall(tool);
  const outcome = callOutcome(tool, desc);
  const verb = callVerb(tool, desc);
  const running = isRunning(tool);
  const result = tool.result ?? '';
  const hasOutput = Boolean(result) || Boolean(tool.previewImage);
  const status = running ? 'running' : tool.status;
  const editPath = desc.path ? resolvePath(desc.path, wsPath).absolute ?? desc.path : null;

  const outcomeCell = outcome === null
    ? <span className="activity-call-out" />
    : outcome.kind === 'diff'
      ? (
        <span className="activity-call-out">
          <DiffPill
            added={outcome.added}
            removed={outcome.removed}
            onOpen={editPath ? () => useUIStore.getState().openEditDiff({ toolUseId: tool.toolId, path: editPath }) : undefined}
          />
        </span>
      )
      : <span className={`activity-call-out${outcome.tone === 'neutral' ? '' : ` ${outcome.tone}`}`}>{outcome.text}</span>;

  const outputChars = tool.meta?.outputChars;
  const shot = tool.previewImage;
  const media = shot ? mediaFor?.(tool) : undefined;

  return (
    <li className={`activity-call status-${status}${tool.status === 'error' ? ' failed' : ''}`}>
      <div className={`activity-call-line${hasOutput ? ' has-output' : ''}`} onClick={hasOutput ? onToggle : undefined}>
        <span className="activity-slot">
          {running ? <Spinner /> : tool.status === 'stopped' ? ICON.stop : iconFor(tool.toolName)}
        </span>
        {desc.kind !== 'card' && <span className="activity-call-verb">{verb}</span>}
        <CallSubject desc={desc} wsPath={wsPath} />
        {outcomeCell}
        {hasOutput ? (
          <button
            type="button"
            className="activity-call-toggle"
            aria-expanded={expanded}
            aria-label={expanded ? 'Hide output' : 'Show output'}
            title={expanded ? 'Hide output' : 'Show output'}
            onClick={(e) => {
              e.stopPropagation();
              onToggle();
            }}
          >
            {ICON.chevronDown}
          </button>
        ) : (
          <span className="activity-slot" />
        )}
      </div>
      {expanded && shot && (
        <div className="activity-call-shot">
          <PreviewScreenshotCard
            mediaType={shot.media_type}
            data={shot.data}
            caption={result || undefined}
            size={media?.size}
            onResize={media?.onResize}
          />
        </div>
      )}
      {expanded && !shot && result && (
        <>
          <pre className="activity-call-raw">{result}</pre>
          {tool.meta?.truncated === true && (
            <div className="activity-call-note">
              {typeof outputChars === 'number' && outputChars > result.length
                ? `Showing the first ${formatCount(result.length)} of ${formatCount(outputChars)} characters.`
                : 'Showing the first part of the output.'}
            </div>
          )}
        </>
      )}
    </li>
  );
}

export const ActivityRow: React.FC<ActivityRowProps> = ({ tools, writtenLabel, mediaFor }) => {
  const wsPath = useUIStore((s) => s.wsPath);
  const desc = useMemo(() => describeGroup(tools, writtenLabel), [tools, writtenLabel]);
  const chips = useMemo(() => groupChips(tools), [tools]);
  const { state } = desc;
  const elapsed = useElapsedSeconds(state === 'running');

  // An error or an unchecked goal opens the group, also when it lands while
  // the row is already on screen; the user's own toggle wins after that. A
  // screenshot opens too: the picture is the one thing its tool exists to show.
  const defaultOpen = state === 'error' || state === 'unchecked' || tools.some((t) => Boolean(t.previewImage));
  const [open, setOpen] = useState(defaultOpen);
  const [wasDefaultOpen, setWasDefaultOpen] = useState(defaultOpen);
  if (defaultOpen !== wasDefaultOpen) {
    setWasDefaultOpen(defaultOpen);
    if (defaultOpen) setOpen(true);
  }

  // Stable per-call keys. Index-prefixed on purpose: a row from an older
  // transcript carries the tool name as its toolId, so two same-named calls
  // would share a key and one click would toggle both.
  const keys = useMemo(() => tools.map((t, i) => `${i}-${t.toolId || t.toolName}`), [tools]);
  // A failed call and a screenshot show their output unless the user hid it.
  const [overrides, setOverrides] = useState<Map<string, boolean>>(() => new Map());
  const expandedByDefault = (t: ToolUseEvent) => Boolean(t.previewImage) || (t.status === 'error' && Boolean(t.result));
  const isExpanded = (i: number) => overrides.get(keys[i]) ?? expandedByDefault(tools[i]);
  const withOutput = tools.map((t, i) => (t.result || t.previewImage ? i : -1)).filter((i) => i >= 0);
  const allExpanded = withOutput.length > 0 && withOutput.every(isExpanded);

  const toggleCall = (i: number) => {
    setOverrides((prev) => new Map(prev).set(keys[i], !(prev.get(keys[i]) ?? expandedByDefault(tools[i]))));
  };
  const toggleAll = () => {
    setOverrides(new Map(withOutput.map((i) => [keys[i], !allExpanded])));
  };

  return (
    <div className={`activity-row activity-${state}${open ? ' open' : ''}`}>
      <div className="activity-head">
        <button
          type="button"
          className="activity-summary"
          aria-expanded={open}
          title={desc.title}
          onClick={() => setOpen((v) => !v)}
        >
          <span className="activity-slot">{stateMark(state, tools)}</span>
          <span className="activity-chevron" aria-hidden="true">{ICON.chevronRight}</span>
          <span className="activity-label">{desc.label}</span>
          {desc.tail && <span className="activity-tail">{desc.tail}</span>}
          {(chips.length > 0 || elapsed !== null) && (
            <span className="activity-meta">
              {chips.map((chip, i) => <ChipView key={i} chip={chip} />)}
              {elapsed !== null && <span className="activity-chip live">{elapsed} s</span>}
            </span>
          )}
        </button>
        {open && withOutput.length > 0 && (
          <button
            type="button"
            className="activity-expand-all"
            onClick={toggleAll}
            title={allExpanded ? 'Hide every output' : 'Show every output'}
          >
            {allExpanded ? 'Collapse all' : 'Expand all'}
          </button>
        )}
      </div>
      {open && (
        <ul className="activity-calls">
          {tools.map((tool, i) => (
            <CallLine
              key={keys[i]}
              tool={tool}
              expanded={isExpanded(i)}
              onToggle={() => toggleCall(i)}
              wsPath={wsPath}
              mediaFor={mediaFor}
            />
          ))}
        </ul>
      )}
    </div>
  );
};
