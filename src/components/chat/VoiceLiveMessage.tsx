import React, { useMemo } from 'react';
import { useVoiceStore } from '@/stores/voiceStore';
import { useSessionStore } from '@/stores/sessionStore';
import { stepsToToolUse } from '@/services/voiceEvents';
import { voiceController } from '@/services/voiceController';
import { ActivityRow } from '@/components/chat/ActivityRow';
import { ACTIVITY_MIN_RUN, AgentCard, groupAgentTools, groupForActivity, isActivityEntry, isTasksEntry } from '@/components/chat/AgentCard';
import { TeamReportCard } from '@/components/chat/TeamReportCard';
import { findMatchingTeamReports } from '@/hooks/chatStream/teamProgress';
import { TaskCard } from '@/components/chat/TaskCard';

/**
 * The assistant bubble for what is happening in voice mode right now: the
 * words Sonic is about to say (the preview, kept until its confirmed copy is
 * committed once the speech has ended), the delegated runs' tool activity and
 * agent cards, and a request card when a run needs the user.
 *
 * Delegated runs outlive the conversation: after the user hangs up, their
 * steps and cards stay here until each run's written answer is committed.
 */
export const VoiceLiveMessage: React.FC = () => {
  const status = useVoiceStore((s) => s.status);
  const liveText = useVoiceStore((s) => s.liveText);
  const steps = useVoiceStore((s) => s.steps);
  const teamReports = useVoiceStore((s) => s.teamReports);
  const draining = useVoiceStore((s) => s.draining);
  const runSessionId = useVoiceStore((s) => s.runSessionId);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const voiceId = useVoiceStore((s) => s.voiceId);
  const voices = useVoiceStore((s) => s.voices);
  const pending = useVoiceStore((s) => s.pendingRequest);

  const tools = useMemo(() => stepsToToolUse(steps.filter((st) => st.source === 'assistant')), [steps]);
  const reportCount = Object.keys(teamReports).length;
  const voiceName = voices.find((v) => v.id === voiceId)?.label ?? voiceId;

  const live = status !== 'off' && status !== 'connecting' && status !== 'ending';
  const hasRunWork = tools.length > 0 || reportCount > 0 || !!pending;
  // Voice work belongs to the session it started in; a new or other session
  // must not show it.
  if (runSessionId && currentSessionId && runSessionId !== currentSessionId) return null;
  if (!live && !hasRunWork) return null;
  if (!liveText && !hasRunWork && status !== 'thinking') return null;
  const showHeader = live && (status === 'speaking' || status === 'thinking');

  return (
    <div className="chat-msg-wrap assistant-wrap" data-testid="voice-live-message">
      <div className="chat-msg assistant voice-live" role="article" aria-live="polite">
        {showHeader && (
          <div className={`speak-row speak-${status}`}>
            <svg width="13" height="13" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none" />
              <path d="M15.54 8.46a5 5 0 0 1 0 7.07" /><path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
            </svg>
            {status === 'speaking' ? `Speaking · ${voiceName}` : 'Thinking'}
            {status === 'speaking' && (
              <span className="voice-eq" aria-hidden="true"><i /><i /><i /><i /><i /></span>
            )}
          </div>
        )}
        {live && liveText && <p>{liveText}</p>}
        {(tools.length > 0 || reportCount > 0) && (
          <VoiceRunActivity tools={tools} teamReports={teamReports} />
        )}
        {!live && draining > 0 && hasRunWork && (
          <div className="voice-draining-note">
            <span className="pulse voice-request-dot" aria-hidden="true" />
            Voice is off; the assistant is still finishing this work.
            <button
              type="button"
              className="voice-request-btn no voice-draining-stop"
              onClick={() => voiceController.cancelRuns()}
              title="Stop the background work"
            >
              Stop
            </button>
          </div>
        )}
        {pending && <VoiceRequestCard request={pending} />}
      </div>
    </div>
  );
};

/** The delegated runs' activity, rendered exactly like a streaming chat turn:
 *  agent groups become the rich per-agent cards when their reports are known,
 *  everything else collapses into the Activity row. */
const VoiceRunActivity: React.FC<{
  tools: ReturnType<typeof stepsToToolUse>;
  teamReports: ReturnType<typeof useVoiceStore.getState>['teamReports'];
}> = ({ tools, teamReports }) => {
  const entries = useMemo(() => groupForActivity(groupAgentTools(tools), { minRun: ACTIVITY_MIN_RUN }), [tools]);
  const agentTools = useMemo(
    () => tools.filter((t) => t.toolName === 'team_create' || t.toolName === 'spawn_agent'),
    [tools],
  );
  const orphanReports = useMemo(
    () =>
      Object.values(teamReports).filter(
        (r) => findMatchingTeamReports(agentTools, { [r.team_id]: r }).length === 0,
      ),
    [teamReports, agentTools],
  );
  return (
    <div className="skill-traces">
      {entries.map((entry, idx) => {
        if (Array.isArray(entry)) {
          const reports = findMatchingTeamReports(entry, teamReports);
          if (reports.length > 0) {
            return reports.map((r) => <TeamReportCard key={`team-${r.team_id}-${idx}`} report={r} />);
          }
          return <AgentCard key={`agent-group-${idx}`} tools={entry} />;
        }
        if (isTasksEntry(entry)) return <TaskCard key={`tasks-${idx}`} tools={entry.tools} />;
        if (isActivityEntry(entry)) return <ActivityRow key={`activity-${idx}`} tools={entry.tools} />;
        return <ActivityRow key={`single-${idx}`} tools={[entry]} />;
      })}
      {orphanReports.map((r) => (
        <TeamReportCard key={`live-team-${r.team_id}`} report={r} />
      ))}
    </div>
  );
};

/** The delegated assistant needs the user: the same information the chat's
 *  approval card shows, answerable by voice (Sonic asks) or by clicking. */
const VoiceRequestCard: React.FC<{ request: NonNullable<ReturnType<typeof useVoiceStore.getState>['pendingRequest']> }> = ({ request }) => {
  const title =
    request.kind === 'approval_request'
      ? 'Approval needed'
      : request.kind === 'user_question'
        ? 'Question for you'
        : 'Folder needed';
  const resolve = (decision: string) => voiceController.resolveRequest(decision);
  return (
    <div className="ws-approval-card voice-request" data-testid="voice-request">
      <div className="voice-request-head">
        <span>{title}</span>
        {request.riskHint && <span className={`risk-badge risk-${request.riskHint}`}>{request.riskHint}</span>}
      </div>
      <div className="voice-request-summary">{request.summary || request.question}</div>
      {request.detail && <div className="voice-request-detail">{request.detail}</div>}
      <div className="voice-request-actions">
        {request.kind === 'approval_request' && (
          <>
            <button type="button" className="voice-request-btn yes" onClick={() => resolve('approve')}>Yes</button>
            <button type="button" className="voice-request-btn no" onClick={() => resolve('deny')}>No</button>
          </>
        )}
        {request.kind === 'user_question' &&
          request.options.map((opt) => (
            <button key={opt} type="button" className="voice-request-btn" onClick={() => resolve(opt)}>{opt}</button>
          ))}
      </div>
      <div className="voice-request-hint">
        <span className="pulse voice-request-dot" aria-hidden="true" />
        {request.kind === 'approval_request'
          ? 'Say yes or no, or click.'
          : request.kind === 'user_question'
            ? 'Say your answer, or click an option.'
            : 'Say the folder name or path.'}
      </div>
    </div>
  );
};
