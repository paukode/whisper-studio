import React from 'react';
import type { ChatMessage, ToolUseEvent } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { ProgramArtifactCard } from '@/components/chat/ProgramArtifactCard';
import { VizCard } from '@/components/chat/VizCard';
import { WorkspacePromptCard } from '@/components/chat/WorkspacePromptCard';
import { AgentCard, groupAgentTools, groupForActivity, isActivityEntry, isTasksEntry, ACTIVITY_MIN_RUN } from '@/components/chat/AgentCard';
import { TaskCard } from '@/components/chat/TaskCard';
import { PlanCard } from '@/components/chat/PlanCard';
import type { SessionTask } from '@/stores/taskStore';
import { ActivityRow } from '@/components/chat/ActivityRow';
import { ICON } from '@/components/chat/activityIcons';
import { TeamAgentRows } from '@/components/chat/AgentRows';
import { WorkflowPreviewCard } from '@/components/chat/WorkflowPreviewCard';
import { WorkflowRunCard } from '@/components/chat/WorkflowRunCard';
import { CIStatusCard } from '@/components/chat/CIStatusCard';
import { CIDiagnosisCard } from '@/components/chat/CIDiagnosisCard';
import { saveMediaSize } from '@/components/chat/MediaResize';
import { findMatchingTeamReports } from '@/hooks/chatStream/teamProgress';
import { UserQuestionCard, UserQuestionGroupCard } from '@/components/chat/UserQuestionCard';
import { AnswerSources } from '@/components/chat/AnswerSources';

export interface TurnSegmentProps {
  /** One run of the turn's rows (traceRuns): the store's own objects, never
   *  copies, so a card answers and a picture saves its size on exactly the
   *  row that holds it. */
  rows: ChatMessage[];
  /** The store index of each row. */
  indices: number[];
  /** Task checkpoints by store index (resolveTaskCheckpoints): the
   *  cumulative list a row's Tasks entry shows, absent when the entry is
   *  suppressed (state unchanged since the previous task row). */
  taskCheckpoints: ReadonlyMap<number, SessionTask[]>;
  /** The store index whose Tasks entry is the current one (the most-recent
   *  checkpoint, bound to the task store), or -1. All other checkpoints are
   *  historical snapshots that must not show a live "in progress" spinner. */
  liveCheckpointIdx: number;
}

/**
 * Everything one run of a turn's rows shows inside the turn's bubble, in
 * order: the thinking block and the grounding chip of its first row, the tool
 * traces of every row as one list, the agent rows, then the prose, question,
 * picture, plan and artifact cards of its last row (traceRuns puts rows in
 * one run only when nothing else is drawn between their traces, so only the
 * first row has anything above them and a run of more than one row ends in a
 * tool-only row). The bubble
 * around it, the Verified chips, the timestamp and the actions belong to the
 * turn (TurnBubble). A fragment, so prose and rows of neighbouring segments
 * are siblings and share one spacing rule.
 */
const TurnSegmentView: React.FC<TurnSegmentProps> = ({ rows, indices, taskCheckpoints, liveCheckpointIdx }) => {
  const head = rows[0];
  const tail = rows[rows.length - 1];
  // Each call's own row: labels, picture sizes and task checkpoints are kept
  // per row, so a group drawn across rows still reads them from there.
  const owner = new Map<ToolUseEvent, number>();
  rows.forEach((r, k) => r.toolUse?.forEach((t) => owner.set(t, k)));
  const toolUse = rows.length === 1 ? (head.toolUse ?? []) : rows.flatMap((r) => r.toolUse ?? []);
  const teamReports = rows.length === 1
    ? head.teamReports
    : rows.reduce<ChatMessage['teamReports']>(
      (acc, r) => (r.teamReports ? { ...(acc ?? {}), ...r.teamReports } : acc),
      undefined,
    );
  const rowOf = (tool: ToolUseEvent | undefined) => rows[(tool && owner.get(tool)) ?? 0];

  // A group's written caption is keyed by its first call's id, on that
  // call's row.
  const writtenLabelOf = (tools: ToolUseEvent[]) => rowOf(tools[0]).activityLabels?.[tools[0]?.toolId ?? ''];
  // A screenshot's dragged size is keyed by the call's place in its row's
  // toolUse, which never changes once the row is committed.
  const mediaFor = (tool: ToolUseEvent) => {
    const row = rowOf(tool);
    const key = `shot-${row.toolUse?.indexOf(tool) ?? -1}`;
    return {
      size: row.mediaSizes?.[key],
      onResize: (size: Parameters<typeof saveMediaSize>[2]) => saveMediaSize(row, key, size),
    };
  };
  // A Tasks entry shows the checkpoint of the rows that hold its calls: the
  // last one they carry, live when one of them is the live row.
  const checkpointOf = (tools: ToolUseEvent[]) => {
    let tasks: SessionTask[] | null = null;
    let live = false;
    for (const k of new Set(tools.map((t) => owner.get(t) ?? 0))) {
      const i = indices[k];
      tasks = taskCheckpoints.get(i) ?? tasks;
      live = live || i === liveCheckpointIdx;
    }
    return { tasks, live };
  };

  return (
    <>
      {head._thinkingText && (
        <details className="thinking-block">
          <summary className="thinking-summary">
            <span className="activity-slot">{ICON.thought}</span>
            Thought process
            {head._thinkingMs != null && head._thinkingMs > 0 && (
              <span className="thinking-elapsed">{(head._thinkingMs / 1000).toFixed(1)}s</span>
            )}
          </summary>
          <div className="thinking-body">{head._thinkingText}</div>
        </details>
      )}

      {/* Index-grounding summary: makes it visible whether this turn was
       *  answered from the workspace index (and how much), so a silent
       *  no-grounding can't be mistaken for "the model couldn't find it".
       *  Only present when at least one index was searched (the backend
       *  omits it otherwise), so users without indexes never see a chip.
       *  When the turn persisted its sources (grounding.id), the chip is a
       *  button that opens the passages behind the answer. */}
      {head.grounding && head.grounding.searched > 0 && (
        <AnswerSources grounding={head.grounding} />
      )}

      {/* Tool traces. Two-pass grouping:
       *  1. ``groupAgentTools`` bundles consecutive agent-orchestration calls
       *     (spawn_agent / send_message / team_*) into a single AgentCard.
       *  2. ``groupForActivity`` then bundles consecutive *individual* tool
       *     calls (read / grep / bash / edit) into a single <ActivityRow>,
       *     one line that names what the group did ("Read 3 files") and
       *     expands inline on click.
       *  Special tools (workspace picker, ask-user) pass through both passes;
       *  the ones with their own card keep it, the rest draw as a row of one.
       *
       *  ACTIVITY_MIN_RUN MUST match StreamingMessage (it does, by
       *  construction: both import the shared constant). Tool grouping is a
       *  pure transform of the rows' toolUse (which is persisted verbatim in
       *  the session's chat_history), so using the same threshold here means
       *  the Activity row that appeared while streaming stays put once the
       *  turn completes AND when an old session is restored. With the
       *  default (2), a 1-tool turn would collapse during streaming then pop
       *  back to a bare chip on commit/reload: the disappearing-row bug. */}
      {toolUse.length > 0 && (
        <div className="skill-traces">
          {groupForActivity(groupAgentTools(toolUse), { minRun: ACTIVITY_MIN_RUN }).map((entry, idx) => {
            if (Array.isArray(entry)) {
              // A group can anchor SEVERAL reports (team_create plus each
              // inline spawn_agent's one-member team): render the agent
              // rows of them all, and the generic AgentCard only when none
              // matched. Rendering both for the same group double-carded
              // every inline spawn.
              const matchedReports = findMatchingTeamReports(entry, teamReports);
              if (matchedReports.length > 0) {
                return matchedReports.map((r) => (
                  <TeamAgentRows key={`team-${r.team_id}-${idx}`} report={r} />
                ));
              }
              return <AgentCard key={`agent-group-${idx}`} tools={entry} />;
            }
            // Task-management run: render the compact Tasks row only at a
            // checkpoint where the cumulative state changed (taskCheckpoint
            // non-null); otherwise suppress it so the same "N/M done" row
            // doesn't repeat on every turn. The full list is in the drawer.
            if (isTasksEntry(entry)) {
              const checkpoint = checkpointOf(entry.tools);
              return checkpoint.tasks && checkpoint.tasks.length > 0
                ? <TaskCard key={`tasks-${idx}`} tasks={checkpoint.tasks} historical={!checkpoint.live} />
                : null;
            }
            // Activity bundle: a run of consecutive non-special tools.
            if (isActivityEntry(entry)) {
              return (
                <ActivityRow
                  key={`activity-${idx}`}
                  tools={entry.tools}
                  writtenLabel={writtenLabelOf(entry.tools)}
                  mediaFor={mediaFor}
                />
              );
            }
            const tool = entry;
            // Workflow runtime cards (WS-D)
            if (tool.toolName === 'workflow_preview') {
              return <WorkflowPreviewCard key={`wf-preview-${idx}`} preview={tool.input as never} />;
            }
            if (tool.toolName === 'workflow_started') {
              const wi = tool.input as { run_id?: string; name?: string };
              return wi.run_id ? <WorkflowRunCard key={`wf-run-${idx}`} runId={wi.run_id} name={wi.name} /> : null;
            }
            // CI watch + autofix cards (WS-J)
            if (tool.toolName === 'ci_started') {
              const ci = tool.input as { task_id?: string; branch?: string };
              return ci.task_id ? <CIStatusCard key={`ci-${idx}`} taskId={ci.task_id} branch={ci.branch} /> : null;
            }
            if (tool.toolName === 'ci_diagnosis') {
              return <CIDiagnosisCard key={`ci-dx-${idx}`} data={tool.input as never} />;
            }
            // Workspace prompt: render interactive folder picker card
            if (tool.toolName === 'ws_workspace_prompt') {
              const input = tool.input as {
                suggested?: string; recent?: string[]; tool_name?: string;
              };
              return (
                <WorkspacePromptCard
                  key={`ws-prompt-${idx}`}
                  suggested={input.suggested ?? ''}
                  recent={input.recent ?? []}
                  toolName={input.tool_name}
                  // tool.toolId now carries the REAL tool_use_id the backend
                  // stashed this pause under (see sseStream.ts): resuming
                  // must answer that exact id, not a hardcoded placeholder.
                  toolUseId={tool.toolId}
                />
              );
            }

            // Default: the same row, for a group of one.
            return (
              <ActivityRow
                key={`${tool.toolName}-${idx}`}
                tools={[tool]}
                writtenLabel={writtenLabelOf([tool])}
                mediaFor={mediaFor}
              />
            );
          })}
        </div>
      )}

      {/* Standalone agent rows: any report NOT anchored to a team_create or
       *  spawn_agent tool-use row above renders directly here. That covers
       *  background /subagent messages (no toolUse at all), question-round
       *  commits where the team trace landed on an earlier message, and
       *  stopped turns that carry only the partial report. */}
      {teamReports &&
        Object.values(teamReports)
          .filter((r) => findMatchingTeamReports(
            toolUse.filter(
              (t) => t.toolName === 'team_create' || t.toolName === 'spawn_agent',
            ),
            { [r.team_id]: r },
          ).length === 0)
          .map((r) => (
            <div className="skill-traces" key={`standalone-team-${r.team_id}`}>
              <TeamAgentRows report={r} />
            </div>
          ))}

      {/* The prose, then the cards it introduces: the question card (the
       *  group component is adaptive: one question renders as answer chips
       *  that submit at once, 2+ step through one form; the single
       *  `userQuestion` field still renders old persisted rows), the
       *  pictures, the plan and the artifact. */}
      {tail.content && <MarkdownRenderer content={tail.content} />}
      {tail.userQuestions && tail.userQuestions.length > 0 ? (
        <UserQuestionGroupCard message={tail} />
      ) : tail.userQuestion ? (
        <UserQuestionCard
          question={tail.userQuestion.question}
          options={tail.userQuestion.options}
          answered={tail.userQuestion.answered}
          message={tail}
        />
      ) : null}
      {tail.visuals?.map((viz, i) => (
        <VizCard
          key={`viz-${i}`}
          viz={viz}
          size={tail.mediaSizes?.[`viz-${i}`]}
          onResize={(size) => saveMediaSize(tail, `viz-${i}`, size)}
        />
      ))}
      {tail.plan && <PlanCard plan={tail.plan} />}
      {tail.programArtifact && (
        <ProgramArtifactCard
          title={tail.programArtifact.title}
          html={tail.programArtifact.html}
          description={tail.programArtifact.description}
        />
      )}
    </>
  );
};

/** Two renders of a run of rows draw the same thing when they hold the same
 *  row objects at the same store indices with the same task checkpoints. The
 *  display fold builds new arrays on every store change, so identity of the
 *  arrays themselves says nothing: rows are immutable store objects, and a
 *  turn that grows by one row re-renders only the run that row joins. */
export function sameRows(
  a: Pick<TurnSegmentProps, 'rows' | 'indices' | 'taskCheckpoints' | 'liveCheckpointIdx'>,
  b: Pick<TurnSegmentProps, 'rows' | 'indices' | 'taskCheckpoints' | 'liveCheckpointIdx'>,
): boolean {
  if (a.rows.length !== b.rows.length) return false;
  for (let k = 0; k < a.rows.length; k += 1) {
    const i = a.indices[k];
    if (a.rows[k] !== b.rows[k] || i !== b.indices[k]) return false;
    if (a.taskCheckpoints.get(i) !== b.taskCheckpoints.get(i)) return false;
    if ((i === a.liveCheckpointIdx) !== (i === b.liveCheckpointIdx)) return false;
  }
  return true;
}

export const TurnSegment = React.memo(TurnSegmentView, sameRows);
