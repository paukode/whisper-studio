import type { ChatMessage } from '@/types/chat';

/**
 * The cards that can land with no user row before them, each with the model
 * tools that also produce it. A slash command (useSlashCommands.tsx: /workflow,
 * /ci, /ci autofix) or the CI card's Autofix button appends them on their own,
 * and without a boundary they would join the bubble of whatever turn came
 * last. The model's own workflow_run, ci_watch and ci_autofix calls return
 * the same cards as side effects in the middle of its turn (sseEventCards
 * commits each one as a row of its own), and those belong to that turn.
 */
const CARD_PRODUCERS: ReadonlyMap<string, ReadonlySet<string>> = new Map([
  ['workflow_preview', new Set(['workflow_run', 'ci_autofix'])],
  ['workflow_started', new Set(['workflow_run'])],
  ['ci_started', new Set(['ci_watch'])],
  ['ci_diagnosis', new Set(['ci_autofix'])],
]);

/** One assistant turn on screen: a maximal run of consecutive assistant rows.
 *  The rows are the store's own objects, untouched, so every card in them
 *  still finds its row (messages.indexOf) and keeps its turnSettings and
 *  tool_use_id. */
export interface TurnEntry {
  kind: 'turn';
  rows: ChatMessage[];
  /** The store index of each row, in order. */
  indices: number[];
  /** The first of them: the index the turn answers to (regenerate). */
  index: number;
}

/** Any other row (user, cron_event, session_message, agent_report,
 *  agent_answer), one to one. */
export interface MessageEntry {
  kind: 'message';
  message: ChatMessage;
  index: number;
}

export type DisplayEntry = TurnEntry | MessageEntry;

/** A row whose only tool rows are workflow and CI cards. */
function isCardRow(m: ChatMessage): boolean {
  const tools = m.toolUse ?? [];
  return tools.length > 0 && tools.every((t) => CARD_PRODUCERS.has(t.toolName));
}

/** True when `caller` holds a call that returns every card in `card`: the
 *  model ran it in this turn, so the card is that call's side effect. */
function producedBy(card: ChatMessage, caller: ChatMessage | null): boolean {
  if (!caller) return false;
  const calls = new Set((caller.toolUse ?? []).map((t) => t.toolName));
  return (card.toolUse ?? []).every((t) => {
    const producers = CARD_PRODUCERS.get(t.toolName);
    return !!producers && [...producers].some((p) => calls.has(p));
  });
}

/**
 * Fold the message list into display entries: one entry per assistant turn,
 * every other row passed through. A turn streams as several store rows (prose
 * before a tool call is committed as its own row, and so is every card), and
 * those rows STAY separate in the store: their indices drive edit, delete,
 * regenerate, the task checkpoints and the deliveries that settle on the last
 * assistant row. Only the display joins them.
 *
 * A workflow or CI card row joins the turn only when the row before it (past
 * any cards that joined the same way) holds the model call that returns that
 * card. Any other card row (a slash command, the Autofix button) starts a new
 * entry instead of joining the turn above it. Pure: rows are never copied or
 * changed.
 */
export function turnEntries(messages: ChatMessage[]): DisplayEntry[] {
  const out: DisplayEntry[] = [];
  let turn: TurnEntry | null = null;
  // The turn's latest row that is not a card: the one whose calls a card
  // that follows can come from.
  let caller: ChatMessage | null = null;
  for (let i = 0; i < messages.length; i += 1) {
    const m = messages[i];
    if (m.role !== 'assistant') {
      turn = null;
      caller = null;
      out.push({ kind: 'message', message: m, index: i });
      continue;
    }
    const card = isCardRow(m);
    if (!turn || (card && !producedBy(m, caller))) {
      turn = { kind: 'turn', rows: [], indices: [], index: i };
      out.push(turn);
      caller = null;
    }
    if (!card) caller = m;
    turn.rows.push(m);
    turn.indices.push(i);
  }
  return out;
}

/** The key a turn's bubble is known by: its first row. Stable while the turn
 *  grows, so a row committed mid-turn never remounts the bubble. */
export function turnKey(entry: TurnEntry): string {
  return `${entry.rows[0].timestamp}-${entry.index}`;
}

/** A turn that holds a spoken row (voice mode): its bubble carries the
 *  spoken mark, and it never replays the entrance, because the live voice
 *  bubble already showed those words. One predicate for both: a turn only
 *  grows, so once spoken it stays spoken, and the entrance class never
 *  comes off (which would restart the animation). */
export function spokenTurn(rows: readonly ChatMessage[]): boolean {
  return rows.some((r) => !!r.spoken);
}

/** Something a row draws above its tool traces: the thinking block or the
 *  grounding chip. */
function drawsBeforeTraces(m: ChatMessage): boolean {
  return !!m._thinkingText || !!(m.grounding && m.grounding.searched > 0);
}

/** Something a row draws below its tool traces, apart from agent rows: the
 *  prose, the question card, the pictures, the plan, the artifact. */
function drawsAfterTraces(m: ChatMessage): boolean {
  return (
    !!(m.content ?? '').trim() ||
    !!(m.userQuestions && m.userQuestions.length > 0) ||
    !!m.userQuestion ||
    !!(m.visuals && m.visuals.length > 0) ||
    !!m.plan ||
    !!m.programArtifact
  );
}

const hasTraces = (m: ChatMessage): boolean => !!m.toolUse && m.toolUse.length > 0;

/**
 * Split a turn's rows into the runs whose tool traces draw as one list, as
 * positions into `rows`. A row joins the run before it when it is only tool
 * traces (nothing drawn above or below them, agent rows aside) and the run's
 * last row draws nothing below its own traces. So a call and the refusal card
 * committed after it (hook_blocked and the other refusals land as rows of
 * their own) fall in one activity group, even under the thinking block of the
 * row that made the call, and the tool-only rows that every agent card leaves
 * behind stack as one list. A row with prose keeps its own list: its calls
 * came after the card that split the stream, and they draw as the next group,
 * edge to edge with the run above.
 */
export function traceRuns(rows: readonly ChatMessage[]): number[][] {
  const runs: number[][] = [];
  for (let k = 0; k < rows.length; k += 1) {
    const prev = k > 0 ? rows[k - 1] : null;
    const m = rows[k];
    const joins =
      !!prev && hasTraces(prev) && !drawsAfterTraces(prev) &&
      hasTraces(m) && !drawsBeforeTraces(m) && !drawsAfterTraces(m);
    if (joins) runs[runs.length - 1].push(k);
    else runs.push([k]);
  }
  return runs;
}

/** The written caption of an activity group drawn from `rows` (one run of
 *  traceRuns), keyed by the group's first toolId. A caption is stored on the
 *  row that committed it, which is the group's last row when the group spans
 *  rows, so every row of the run is read, the latest first: a group that
 *  spans rounds (rows saved before the skill frames named a round) and got
 *  captions from several of them shows the last one. */
export function captionIn(rows: readonly ChatMessage[], key: string): string | undefined {
  for (let k = rows.length - 1; k >= 0; k -= 1) {
    const label = rows[k].activityLabels?.[key];
    if (label) return label;
  }
  return undefined;
}
