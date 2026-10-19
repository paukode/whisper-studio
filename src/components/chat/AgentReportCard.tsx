/**
 * AgentReportCard: reports from agents that finished with no live turn to
 * carry them (a team whose launching turn was cancelled, a background agent,
 * or an agent resumed after it had stopped). The backend shows the same
 * content to the model as a user turn on the next message.
 *
 * Drawn in the activity rows' family (ActivityRow.tsx, modules/activity.css)
 * inside a card at an assistant turn's width (ChatMessage.tsx wraps it).
 * Every line is one button: icon slot, chevron, label, mono tail, then the
 * chips on the right. Collapsed by default. One agent is one line that opens
 * to its report; several are one line for the team that opens to a line per
 * agent, each opening to its report.
 *
 * Until the user opens a report once, its line carries the report icon in
 * the accent colour and a New chip. "Seen" is kept per viewer in
 * localStorage, one key per report agent: the team id, the report's
 * timestamp and the agent id. The timestamp tells apart the replies of one
 * resumed agent, which all carry the same team id and agent id. Only the
 * most recent keys are kept; with no storage the cue lasts until the report
 * is opened in this view.
 */
import React, { useState } from 'react';
import type { AgentReportPayload, AgentReportRow } from '@/types/chat';
import { MarkdownRenderer } from '@/components/markdown/MarkdownRenderer';
import { ICON } from '@/components/chat/activityIcons';
import { useDockStore } from '@/stores/dockStore';
import { STORAGE_KEYS } from '@/utils/storageKeys';

interface Props {
  report: AgentReportPayload;
}

type Tone = 'done' | 'err' | 'neutral';

/** How an agent stopped, as a chip: by its stop reason, or its status when
 *  none was recorded. A finished run takes the done tone and a failure the
 *  error tone; a limit or a cancel stays neutral (its report was written). */
const OUTCOME: Record<string, { text: string; tone: Tone }> = {
  completed: { text: 'finished', tone: 'done' },
  turn_limit: { text: 'turn limit', tone: 'neutral' },
  deadline: { text: 'time limit', tone: 'neutral' },
  cost_cap: { text: 'cost cap', tone: 'neutral' },
  refused_calls: { text: 'refused calls', tone: 'neutral' },
  cancelled: { text: 'cancelled', tone: 'neutral' },
  error: { text: 'failed', tone: 'err' },
  failed: { text: 'failed', tone: 'err' },
  stopped: { text: 'stopped', tone: 'neutral' },
};

function agentOutcome(agent: AgentReportRow): { text: string; tone: Tone } {
  const key = agent.stop_reason ?? agent.status;
  return OUTCOME[key] ?? { text: key, tone: 'neutral' };
}

function chipClass(tone: Tone): string {
  return tone === 'neutral' ? 'activity-chip' : `activity-chip ${tone}`;
}

/** The agent's name, or its type when the name is only its task's first
 *  line (a spawned agent is labelled by its task), so the line does not say
 *  the task twice: once as the label and again as the tail. */
function agentName(agent: AgentReportRow): string {
  const name = (agent.name ?? '').trim();
  if (!name) return agent.agent_type;
  const firstLine = (agent.task ?? '').trim().split('\n')[0].trim();
  const cut = name.endsWith('…') ? name.slice(0, -1) : null;
  const fromTask = name === firstLine || (cut !== null && cut.length > 0 && firstLine.startsWith(cut));
  return fromTask ? agent.agent_type : name;
}

/** One report agent: its team, the report's timestamp (a resumed agent
 *  sends each reply with the same team id and agent id) and the agent. */
function seenKey(report: AgentReportPayload, agent: AgentReportRow, index: number): string {
  return `${report.team_id}:${report.timestamp ?? ''}:${agent.agent_id ?? agent.name ?? index}`;
}

/** Seen keys kept in storage, oldest first. The cap keeps the list small;
 *  past it, the report opened longest ago shows New again. */
const SEEN_CAP = 500;

function readSeenKeys(): string[] {
  try {
    const list: unknown = JSON.parse(localStorage.getItem(STORAGE_KEYS.AGENT_REPORT_SEEN) ?? '[]');
    return Array.isArray(list) ? list.filter((k): k is string => typeof k === 'string') : [];
  } catch {
    return [];
  }
}

function rememberSeen(key: string): void {
  try {
    const list = readSeenKeys().filter((k) => k !== key);
    list.push(key);
    localStorage.setItem(STORAGE_KEYS.AGENT_REPORT_SEEN, JSON.stringify(list.slice(-SEEN_CAP)));
  } catch {
    /* No storage: the report counts as seen in this view only. */
  }
}

interface LineProps {
  label: string;
  tail?: string;
  title?: string;
  chips: React.ReactNode;
  open: boolean;
  unseen: boolean;
  failed: boolean;
  onToggle: () => void;
}

function ReportLine({ label, tail, title, chips, open, unseen, failed, onToggle }: LineProps) {
  const state = `${unseen ? ' unseen' : ''}${failed ? ' failed' : ''}`;
  return (
    <div className="activity-head">
      <button
        type="button"
        className={`activity-summary agent-report-line${state}`}
        aria-expanded={open}
        title={title || undefined}
        onClick={onToggle}
      >
        <span className="activity-slot">{ICON.inbox}</span>
        <span className="activity-chevron" aria-hidden="true">{ICON.chevronRight}</span>
        <span className="activity-label">{label}</span>
        {tail && <span className="activity-tail">{tail}</span>}
        <span className="activity-meta">
          {unseen && <span className="activity-chip agent-report-new">New</span>}
          {chips}
        </span>
      </button>
    </div>
  );
}

function OutcomeChip({ agent }: { agent: AgentReportRow }) {
  const { text, tone } = agentOutcome(agent);
  const n = agent.turns_used;
  const rounds = n ? ` · ${n} round${n === 1 ? '' : 's'}` : '';
  return <span className={chipClass(tone)}>{text + rounds}</span>;
}

/** The team line's chips: how many finished, and how many failed. A team
 *  line always has agents; ChatMessage draws no card for a report without. */
function TeamChips({ agents }: { agents: AgentReportRow[] }) {
  const tones = agents.map((a) => agentOutcome(a).tone);
  const finished = tones.filter((t) => t === 'done').length;
  const failed = tones.filter((t) => t === 'err').length;
  if (finished === agents.length) {
    return <span className={chipClass('done')}>{finished} finished</span>;
  }
  return (
    <>
      <span className={chipClass('neutral')}>{finished} of {agents.length} finished</span>
      {failed > 0 && <span className={chipClass('err')}>{failed} failed</span>}
    </>
  );
}

function ReportBody({ agent, reason }: { agent: AgentReportRow; reason?: string }) {
  const agentId = agent.agent_id;
  return (
    <div className="agent-report-body">
      {reason && <div className="agent-report-reason">{reason}</div>}
      <div className="agent-report-text">
        <MarkdownRenderer content={agent.result || '(no report)'} />
      </div>
      {agentId && (
        <button
          type="button"
          className="agent-report-open"
          onClick={() => useDockStore.getState().openAgent(agentId, agent.name ?? agent.agent_type)}
        >
          Open in the agent pane
        </button>
      )}
    </div>
  );
}

export const AgentReportCard: React.FC<Props> = ({ report }) => {
  const agents = report.agents ?? [];
  const keys = agents.map((a, i) => seenKey(report, a, i));
  const [seen, setSeen] = useState<ReadonlySet<string>>(() => {
    const stored = new Set(readSeenKeys());
    return new Set(keys.filter((k) => stored.has(k)));
  });
  const [openAgents, setOpenAgents] = useState<ReadonlySet<number>>(() => new Set());
  const [teamOpen, setTeamOpen] = useState(false);

  // Opening a report marks it seen, here and in storage; closing it does not
  // bring the cue back.
  const toggleAgent = (i: number) => {
    const opening = !openAgents.has(i);
    setOpenAgents((prev) => {
      const next = new Set(prev);
      if (opening) next.add(i);
      else next.delete(i);
      return next;
    });
    if (opening && !seen.has(keys[i])) {
      rememberSeen(keys[i]);
      setSeen((prev) => new Set(prev).add(keys[i]));
    }
  };
  const failed = (a: AgentReportRow) => agentOutcome(a).tone === 'err';

  if (agents.length === 1) {
    const agent = agents[0];
    // The tail cuts a long task short and the body does not repeat it, so
    // the hover text carries the whole task, then the reason.
    return (
      <div className="agent-report-card" data-testid="agent-report-card">
        <ReportLine
          label={`Report from ${agentName(agent)}`}
          tail={agent.task}
          title={[agent.task, report.reason].filter(Boolean).join('\n\n')}
          chips={<OutcomeChip agent={agent} />}
          open={openAgents.has(0)}
          unseen={!seen.has(keys[0])}
          failed={failed(agent)}
          onToggle={() => toggleAgent(0)}
        />
        {openAgents.has(0) && <ReportBody agent={agent} reason={report.reason} />}
      </div>
    );
  }

  return (
    <div className="agent-report-card" data-testid="agent-report-card">
      <ReportLine
        label={`Reports from ${agents.length} agents`}
        tail={report.team_name}
        title={report.reason}
        chips={<TeamChips agents={agents} />}
        open={teamOpen}
        unseen={keys.some((k) => !seen.has(k))}
        failed={agents.some(failed)}
        onToggle={() => setTeamOpen((v) => !v)}
      />
      {teamOpen && (
        <div className="agent-report-team">
          {report.reason && <div className="agent-report-reason">{report.reason}</div>}
          {agents.map((agent, i) => (
            <div key={`${i}-${keys[i]}`} className="agent-report-agent">
              <ReportLine
                label={agentName(agent)}
                tail={agent.task}
                title={agent.task}
                chips={<OutcomeChip agent={agent} />}
                open={openAgents.has(i)}
                unseen={!seen.has(keys[i])}
                failed={failed(agent)}
                onToggle={() => toggleAgent(i)}
              />
              {openAgents.has(i) && <ReportBody agent={agent} />}
            </div>
          ))}
        </div>
      )}
    </div>
  );
};
