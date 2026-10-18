import React, { useCallback, useMemo } from 'react';
import type { ChatMessage, Delivery } from '@/types/chat';
import type { SessionTask } from '@/stores/taskStore';
import { TurnSegment, sameRows } from '@/components/chat/TurnSegment';
import { traceRuns, spokenTurn } from '@/components/chat/turnEntries';
import { DeliveryChips } from '@/components/chat/DeliveryChips';
import { mergeDeliveries } from '@/hooks/chatStream/deliveries';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import { formatMessageTimestamp } from '@/utils/formatTimestamp';
import { exportTurn, copyRichText, turnText } from '@/components/chat/messageActions';

const NO_CHECKPOINTS: ReadonlyMap<number, SessionTask[]> = new Map();

export interface TurnBubbleProps {
  /** The turn's assistant rows, the store's own objects, in order. */
  rows: ChatMessage[];
  /** The store index of each row. */
  indices: number[];
  /** Task checkpoints by store index (resolveTaskCheckpoints). */
  taskCheckpoints?: ReadonlyMap<number, SessionTask[]>;
  /** The store index whose Tasks entry renders live, or -1. */
  liveCheckpointIdx?: number;
  /** Suppress the msgIn entrance: the turn already animated in as the live
   *  reply, and a bubble that remounts must not replay it. */
  noEnter?: boolean;
  /** Set while this turn still streams: draws the live tail (the reply being
   *  written right now) inside this bubble, after the committed rows. It gets
   *  the deliveries the committed rows carry so the tail shows the turn's
   *  Verified chips once, merged with the ones still arriving. The bubble
   *  never subscribes to the stream itself: the tail does. */
  renderLive?: (priorDeliveries: readonly Delivery[]) => React.ReactNode;
}

/** Every row's verified deliveries, merged by kind and target in first-seen
 *  order: the turn shows one Verified row. */
function turnDeliveries(rows: readonly ChatMessage[]): Delivery[] {
  return rows.reduce<Delivery[]>(
    (acc, r) => (r.deliveries && r.deliveries.length > 0 ? mergeDeliveries(acc, r.deliveries) : acc),
    [],
  );
}

const ICON_PROPS = {
  width: 12, height: 12, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 2, strokeLinecap: 'round',
} as const;

/**
 * One assistant turn, one bubble. The turn's rows (prose before a tool call,
 * the tool work, every card) render in order as TurnSegments, one for each
 * run of rows whose tool traces draw as one list (traceRuns); after the last
 * one, once per turn: the Verified chips over every row's deliveries, the
 * spoken mark when any row was spoken, the last row's timestamp, and the
 * actions. Copy takes the
 * prose of all rows, Regenerate re-sends the user message the turn answers,
 * Delete removes every row of the turn.
 *
 * Structure:
 *   div.chat-msg-wrap.assistant-wrap
 *     div.chat-msg.assistant
 *       (segments) (live tail) (chips) (timestamp) (actions)
 */
const TurnBubbleView: React.FC<TurnBubbleProps> = ({
  rows, indices, taskCheckpoints = NO_CHECKPOINTS, liveCheckpointIdx = -1, noEnter, renderLive,
}) => {
  const last = rows[rows.length - 1];
  const deliveries = turnDeliveries(rows);
  const first = indices[0];
  const runs = useMemo(() => traceRuns(rows), [rows]);

  const handleRegenerate = useCallback(() => {
    // Re-send the user message this turn answers, with its attachments, so
    // the regenerate sees the same files.
    const messages = getActiveChatStore().getState().messages;
    const prevUserMsg = messages.slice(0, first).reverse().find((m) => m.role === 'user');
    if (!prevUserMsg) return;
    const prevIndex = messages.indexOf(prevUserMsg);
    window.dispatchEvent(new CustomEvent('whisper-regenerate', {
      detail: {
        index: prevIndex + 1,
        content: prevUserMsg.content,
        attachmentIds: prevUserMsg.attachmentIds,
        attachmentNames: prevUserMsg.attachmentNames,
      },
    }));
  }, [first]);

  const handleDelete = useCallback(() => {
    getActiveChatStore().getState().deleteMessages(indices);
  }, [indices]);

  return (
    <div className="chat-msg-wrap assistant-wrap">
      <div className={`chat-msg assistant${noEnter ? ' no-enter' : ''}`}>
        {runs.map((run) => (
          // Keyed by the run's first row, so a row that joins the last run
          // re-renders that run and never remounts it.
          <TurnSegment
            key={`${rows[run[0]].timestamp}-${run[0]}`}
            rows={run.map((k) => rows[k])}
            indices={run.map((k) => indices[k])}
            taskCheckpoints={taskCheckpoints}
            liveCheckpointIdx={liveCheckpointIdx}
          />
        ))}

        {renderLive ? (
          // The turn is still being written: the tail carries the chips, and
          // the timestamp and actions wait for the turn to end.
          renderLive(deliveries)
        ) : (
          <>
            {/* What the server verified this turn delivered, under the text
             *  that claimed it (the live tail showed the same chips). */}
            {deliveries.length > 0 && <DeliveryChips deliveries={deliveries} />}

            {last.timestamp && (
              <span className="msg-timestamp">
                {spokenTurn(rows) && (
                  <span className="msg-spoken" title="Spoken in voice mode">
                    <svg width="10" height="10" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round"><rect x="9" y="3" width="6" height="11" rx="3"/><path d="M19 11a7 7 0 0 1-14 0"/><line x1="12" y1="18" x2="12" y2="22"/></svg>
                    spoken ·{' '}
                  </span>
                )}
                {formatMessageTimestamp(last.timestamp)}
              </span>
            )}

            <div className="msg-actions">
              <button className="msg-action-btn" onClick={() => void navigator.clipboard.writeText(turnText(rows))} title="Copy">
                <svg {...ICON_PROPS}>
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                </svg>
              </button>
              <button className="msg-action-btn" onClick={() => void copyRichText(turnText(rows))} title="Copy Rich Text">
                <svg {...ICON_PROPS}>
                  <rect x="9" y="9" width="13" height="13" rx="2" ry="2"/><path d="M5 15H4a2 2 0 0 1-2-2V4a2 2 0 0 1 2-2h9a2 2 0 0 1 2 2v1"/>
                  <polyline points="15 14 17 16 21 12"/>
                </svg>
              </button>
              <button className="msg-action-btn" onClick={handleRegenerate} title="Regenerate">
                <svg {...ICON_PROPS}>
                  <polyline points="23 4 23 10 17 10"/><path d="M20.49 15a9 9 0 1 1-2.12-9.36L23 10"/>
                </svg>
              </button>
              <button className="msg-action-btn" onClick={handleDelete} title="Delete">
                <svg {...ICON_PROPS}>
                  <polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/>
                </svg>
              </button>
              <button className="msg-action-btn" onClick={() => exportTurn(rows)} title="Export">
                <svg {...ICON_PROPS}>
                  <path d="M21 15v4a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2v-4"/><polyline points="7 10 12 15 17 10"/><line x1="12" y1="15" x2="12" y2="3"/>
                </svg>
              </button>
            </div>
          </>
        )}
      </div>
    </div>
  );
};

/** Two renders of a turn draw the same thing when they hold the same rows
 *  (sameRows) and the same entrance and live tail: a long session must not
 *  re-render every turn when one row is added at the end. */
function sameTurn(a: TurnBubbleProps, b: TurnBubbleProps): boolean {
  if (a.noEnter !== b.noEnter || a.renderLive !== b.renderLive) return false;
  return sameRows(
    { ...a, taskCheckpoints: a.taskCheckpoints ?? NO_CHECKPOINTS, liveCheckpointIdx: a.liveCheckpointIdx ?? -1 },
    { ...b, taskCheckpoints: b.taskCheckpoints ?? NO_CHECKPOINTS, liveCheckpointIdx: b.liveCheckpointIdx ?? -1 },
  );
}

export const TurnBubble = React.memo(TurnBubbleView, sameTurn);
