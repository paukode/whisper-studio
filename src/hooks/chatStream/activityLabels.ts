/**
 * Model-written captions for activity groups: the activity_label frame
 * (server/chat/engine/activity_labels.py).
 *
 * The server keys a caption by the first labelled tool_use_id of one model
 * round and lists every call of that round. The committed view reads a
 * group's caption from ChatMessage.activityLabels under the group's FIRST
 * toolId, and a group can span several rounds and, once groupToolOnlyRuns
 * folds consecutive tool-only rows, several rows. So a caption is keyed here
 * by the first toolId of the group, as the committed view draws it, that
 * holds one of the round's calls. A group that gets captions from several
 * rounds keeps the last one.
 *
 * A caption for calls still streaming waits in the stream state and rides on
 * the message that commits them (labelCommit). A caption for calls already
 * committed patches their row in place and saves the session
 * (patchCommittedLabel). Until a caption lands, and when none comes (Local
 * mode, a refusal, an error, a timeout), the row keeps its deterministic
 * label.
 */
import { ACTIVITY_MIN_RUN, groupAgentTools, groupForActivity, isActivityEntry } from '@/components/chat/AgentCard';
import { isToolOnly } from '@/components/chat/groupToolOnlyRuns';
import { getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import type { ChatMessage, SSEEventData, ToolUseEvent } from '@/types/chat';

/** One caption: the calls of the round it names, and its words. */
export interface WrittenLabel {
  ids: string[];
  label: string;
}

/** The caption an activity_label frame carries, or null when it has no
 *  words or no call to name. */
export function writtenLabelFromFrame(frame: NonNullable<SSEEventData['activity_label']>): WrittenLabel | null {
  const ids = (frame.tool_use_ids?.length ? frame.tool_use_ids : [frame.tool_use_id]).filter(Boolean);
  const label = frame.label.trim();
  return label && ids.length > 0 ? { ids, label } : null;
}

/** Whether `label` names one of the calls in `toolUse`. */
export function namesCallIn(label: WrittenLabel, toolUse: readonly ToolUseEvent[] | undefined): boolean {
  return !!toolUse && toolUse.some((t) => label.ids.includes(t.toolId));
}

/** The first toolId of the activity group, as the committed view groups
 *  `toolUse`, that holds one of `ids`; null when no group holds one. */
export function groupKeyFor(toolUse: ToolUseEvent[], ids: readonly string[]): string | null {
  for (const entry of groupForActivity(groupAgentTools(toolUse), { minRun: ACTIVITY_MIN_RUN })) {
    if (isActivityEntry(entry) && entry.tools.some((t) => ids.includes(t.toolId))) {
      return entry.tools[0].toolId;
    }
  }
  return null;
}

/** The calls the committed view draws in one entry with `message`'s, in
 *  order: when it is tool-only, the tool-only rows right above it come first
 *  (groupToolOnlyRuns folds such a run into one entry). `before` are the
 *  store rows above it. */
function drawnWith(message: ChatMessage, before: readonly ChatMessage[]): ToolUseEvent[] {
  const own = message.toolUse ?? [];
  if (!isToolOnly(message)) return own;
  let start = before.length;
  while (start > 0 && isToolOnly(before[start - 1])) start -= 1;
  return [...before.slice(start).flatMap((m) => m.toolUse ?? []), ...own];
}

/** `message` with `labels` keyed onto it in order, so a later caption for
 *  the same group replaces an earlier one. */
function withLabels(message: ChatMessage, before: readonly ChatMessage[], labels: readonly WrittenLabel[]): ChatMessage {
  const drawn = drawnWith(message, before);
  let activityLabels = message.activityLabels;
  for (const written of labels) {
    const key = groupKeyFor(drawn, written.ids);
    if (key) activityLabels = { ...activityLabels, [key]: written.label };
  }
  return activityLabels === message.activityLabels ? message : { ...message, activityLabels };
}

/** Put the pending captions that name one of `message`'s calls on it as it
 *  commits, and take them out of `pending`. `before` are the store rows the
 *  message is appended below. */
export function labelCommit(message: ChatMessage, before: readonly ChatMessage[], pending: WrittenLabel[]): ChatMessage {
  const mine = pending.filter((written) => namesCallIn(written, message.toolUse));
  if (mine.length === 0) return message;
  for (const written of mine) pending.splice(pending.indexOf(written), 1);
  return withLabels(message, before, mine);
}

/** Put a caption on the latest committed assistant row that holds one of
 *  its calls, in place, and save explicitly: the row count does not change,
 *  which is what the runtime's own save trigger keys on (the same as
 *  approvalLeg's patchApprovedRow). A caption whose calls are gone (the row
 *  was deleted) is dropped. */
export function patchCommittedLabel(sessionId: string, written: WrittenLabel): void {
  const chat = getChatStore(sessionId);
  const messages = chat.getState().messages;
  let idx = messages.length - 1;
  while (idx >= 0 && !(messages[idx].role === 'assistant' && namesCallIn(written, messages[idx].toolUse))) idx -= 1;
  if (idx < 0) return;
  const patched = withLabels(messages[idx], messages.slice(0, idx), [written]);
  if (patched === messages[idx]) return;
  const next = messages.slice();
  next[idx] = patched;
  chat.getState().setMessages(next);
  useSessionStore.getState().debouncedSave(sessionId);
}
