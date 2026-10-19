/**
 * Render contract for the agent report card: a report that landed with no
 * live turn reads as an activity row (collapsed, label, mono tail, chips),
 * sits at an assistant turn's width, and carries a New cue until the user
 * opens it once.
 */
import { fireEvent, render, within } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { AgentReportCard } from './AgentReportCard';
import { ChatMessage } from './ChatMessage';
import { useDockStore } from '@/stores/dockStore';
import { STORAGE_KEYS } from '@/utils/storageKeys';
import type { AgentReportPayload, AgentReportRow, ChatMessage as ChatMessageType } from '@/types/chat';

function agent(over: Partial<AgentReportRow> = {}): AgentReportRow {
  return {
    name: 'explore',
    agent_id: 'a1',
    agent_type: 'explore',
    task: 'Find where the session list is loaded',
    result: 'The list loads in **sessionStore.ts**.',
    status: 'completed',
    stop_reason: 'completed',
    turns_used: 15,
    ...over,
  };
}

function report(over: Partial<AgentReportPayload> = {}): AgentReportPayload {
  return {
    team_id: 't1',
    team_name: 'Background agent: Find where the session list is loaded',
    reason: 'ran in the background; the turn that launched it did not wait',
    agents: [agent()],
    ...over,
  };
}

const team = () =>
  report({
    team_id: 't2',
    team_name: 'audit',
    reason: 'the turn that launched this team ended before it could read the results',
    agents: [
      agent({ name: 'Probe', agent_id: 'p1', task: 'probe the org', result: 'Probe found two orgs.' }),
      agent({ name: 'Scan', agent_id: 's1', task: 'scan the repo', result: 'Scan is clean.' }),
      agent({ name: 'Lint', agent_id: 'l1', task: 'lint the code', result: 'Lint failed.', status: 'failed', stop_reason: 'error' }),
    ],
  });

const lines = (root: HTMLElement) => [...root.querySelectorAll<HTMLButtonElement>('button.agent-report-line')];

describe('AgentReportCard, one agent', () => {
  beforeEach(() => {
    useDockStore.setState({ panels: [], sizes: [], open: false });
  });

  it('starts collapsed as one line: label, task as the tail, outcome chip', () => {
    const { container } = render(<AgentReportCard report={report()} />);
    const [line] = lines(container);
    expect(lines(container)).toHaveLength(1);
    expect(line.getAttribute('aria-expanded')).toBe('false');
    expect(line.querySelector('.activity-label')?.textContent).toBe('Report from explore');
    expect(line.querySelector('.activity-tail')?.textContent).toBe('Find where the session list is loaded');
    expect([...line.querySelectorAll('.activity-chip')].map((c) => c.textContent)).toEqual(['New', 'finished · 15 rounds']);
    expect(line.querySelector('.activity-chip.done')?.textContent).toBe('finished · 15 rounds');
    // The report and the reason wait behind the chevron. The hover text holds
    // the whole task (the tail cuts it short) and then the reason.
    expect(container.querySelector('.agent-report-body')).toBeNull();
    expect(container.textContent).not.toContain('sessionStore.ts');
    expect(line.title).toBe(
      'Find where the session list is loaded\n\nran in the background; the turn that launched it did not wait',
    );
  });

  it('opens to the reason, the report and the agent pane control', () => {
    const { container, getByText } = render(<AgentReportCard report={report()} />);
    fireEvent.click(lines(container)[0]);
    expect(lines(container)[0].getAttribute('aria-expanded')).toBe('true');
    const body = container.querySelector('.agent-report-body') as HTMLElement;
    expect(body.querySelector('.agent-report-reason')?.textContent).toBe(
      'ran in the background; the turn that launched it did not wait',
    );
    expect(body.querySelector('.agent-report-text strong')?.textContent).toBe('sessionStore.ts');
    expect(container.textContent).not.toContain('task_output');

    fireEvent.click(getByText('Open in the agent pane'));
    const { panels, open } = useDockStore.getState();
    expect(open).toBe(true);
    expect(panels.map((p) => [p.id, p.kind, p.title])).toEqual([['agent:a1', 'agent', 'explore']]);
  });

  it('offers no agent pane for a report without an agent id', () => {
    const { container, queryByText } = render(<AgentReportCard report={report({ agents: [agent({ agent_id: undefined })] })} />);
    fireEvent.click(lines(container)[0]);
    expect(queryByText('Open in the agent pane')).toBeNull();
  });

  it('shows New until the report is opened once, also after a remount', () => {
    const first = render(<AgentReportCard report={report()} />);
    const line = lines(first.container)[0];
    expect(line.classList.contains('unseen')).toBe(true);
    expect(line.querySelector('.agent-report-new')?.textContent).toBe('New');

    fireEvent.click(line);
    expect(line.querySelector('.agent-report-new')).toBeNull();
    expect(line.classList.contains('unseen')).toBe(false);
    // Closing it again does not bring the cue back.
    fireEvent.click(line);
    expect(line.querySelector('.agent-report-new')).toBeNull();
    first.unmount();

    const again = render(<AgentReportCard report={report()} />);
    expect(again.container.querySelector('.agent-report-new')).toBeNull();
    // Seen is per report: another team's report with the same agent id is new.
    const other = render(<AgentReportCard report={report({ team_id: 't9' })} />);
    expect(other.container.querySelector('.agent-report-new')).not.toBeNull();
  });

  it('shows New on each reply of a resumed agent, though all share its team id and agent id', () => {
    const reply = (timestamp: string, result: string) =>
      report({ team_id: 'agent-123', timestamp, agents: [agent({ agent_id: 'agent-123', result })] });
    const first = render(<AgentReportCard report={reply('2026-10-19T09:30:00Z', 'First reply.')} />);
    fireEvent.click(lines(first.container)[0]);
    first.unmount();

    const seenAgain = render(<AgentReportCard report={reply('2026-10-19T09:30:00Z', 'First reply.')} />);
    expect(seenAgain.container.querySelector('.agent-report-new')).toBeNull();
    const next = render(<AgentReportCard report={reply('2026-10-19T10:05:00Z', 'Second reply.')} />);
    expect(next.container.querySelector('.agent-report-new')?.textContent).toBe('New');
  });

  it('keeps only the most recent seen keys in storage', () => {
    const old = Array.from({ length: 500 }, (_, i) => `old-${i}`);
    localStorage.setItem(STORAGE_KEYS.AGENT_REPORT_SEEN, JSON.stringify(old));
    const { container } = render(<AgentReportCard report={report()} />);
    fireEvent.click(lines(container)[0]);
    const kept = JSON.parse(localStorage.getItem(STORAGE_KEYS.AGENT_REPORT_SEEN) ?? '[]') as string[];
    // The list stays at its cap: the oldest key goes, the opened report's key is last.
    expect(kept).toHaveLength(old.length);
    expect(kept[0]).toBe(old[1]);
    expect(kept).not.toContain(old[0]);
    expect(kept[kept.length - 1]).not.toMatch(/^old-/);
    const again = render(<AgentReportCard report={report()} />);
    expect(again.container.querySelector('.agent-report-new')).toBeNull();
  });

  it('keeps the cue for this view when storage is unavailable', () => {
    const get = vi.spyOn(localStorage, 'getItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    const set = vi.spyOn(localStorage, 'setItem').mockImplementation(() => {
      throw new Error('blocked');
    });
    try {
      const { container } = render(<AgentReportCard report={report()} />);
      expect(container.querySelector('.agent-report-new')).not.toBeNull();
      fireEvent.click(lines(container)[0]);
      expect(container.querySelector('.agent-report-new')).toBeNull();
      expect(container.querySelector('.agent-report-body')).not.toBeNull();
    } finally {
      get.mockRestore();
      set.mockRestore();
    }
  });

  it('gives a failed agent the error tone and a limit the neutral one', () => {
    const failed = render(<AgentReportCard report={report({ agents: [agent({ status: 'failed', stop_reason: 'error', turns_used: 1 })] })} />);
    const line = lines(failed.container)[0];
    expect(line.classList.contains('failed')).toBe(true);
    expect(line.querySelector('.activity-chip.err')?.textContent).toBe('failed · 1 round');

    const limit = render(<AgentReportCard report={report({ agents: [agent({ status: 'stopped', stop_reason: 'deadline', turns_used: 0 })] })} />);
    const chip = lines(limit.container)[0].querySelector('.activity-chip:not(.agent-report-new)');
    expect(chip?.textContent).toBe('time limit');
    expect(chip?.className).toBe('activity-chip');
    expect(lines(limit.container)[0].classList.contains('failed')).toBe(false);
  });

  it('names a spawned agent by its type when its name is only its task', () => {
    const task = 'Trace the save path from the composer to the store and report every write';
    const label = `${task.slice(0, 59)}…`;
    const { container } = render(
      <AgentReportCard report={report({ team_name: label, agents: [agent({ name: label, task, agent_type: 'general' })] })} />,
    );
    const line = lines(container)[0];
    expect(line.querySelector('.activity-label')?.textContent).toBe('Report from general');
    expect(line.querySelector('.activity-tail')?.textContent).toBe(task);
  });
});

describe('AgentReportCard, several agents', () => {
  afterEach(() => {
    useDockStore.setState({ panels: [], sizes: [], open: false });
  });

  it('starts as one team line that opens to a line per agent, each opening to its report', () => {
    const { container } = render(<AgentReportCard report={team()} />);
    expect(lines(container)).toHaveLength(1);
    const head = lines(container)[0];
    expect(head.querySelector('.activity-label')?.textContent).toBe('Reports from 3 agents');
    expect(head.querySelector('.activity-tail')?.textContent).toBe('audit');
    expect([...head.querySelectorAll('.activity-chip')].map((c) => c.textContent)).toEqual(['New', '2 of 3 finished', '1 failed']);
    expect(head.querySelector('.activity-chip.err')?.textContent).toBe('1 failed');

    fireEvent.click(head);
    const teamBody = container.querySelector('.agent-report-team') as HTMLElement;
    expect(teamBody.querySelector('.agent-report-reason')?.textContent).toBe(
      'the turn that launched this team ended before it could read the results',
    );
    const agentLines = lines(teamBody);
    expect(agentLines.map((l) => l.querySelector('.activity-label')?.textContent)).toEqual(['Probe', 'Scan', 'Lint']);
    expect(agentLines.map((l) => l.querySelector('.activity-tail')?.textContent)).toEqual(['probe the org', 'scan the repo', 'lint the code']);
    expect(agentLines.every((l) => l.getAttribute('aria-expanded') === 'false')).toBe(true);
    expect(agentLines[2].querySelector('.activity-chip.err')?.textContent).toBe('failed · 15 rounds');
    expect(container.querySelector('.agent-report-body')).toBeNull();

    fireEvent.click(agentLines[1]);
    const bodies = container.querySelectorAll('.agent-report-body');
    expect(bodies).toHaveLength(1);
    expect(bodies[0].textContent).toContain('Scan is clean.');
    // The reason is said once, at the top of the team's list.
    expect(bodies[0].querySelector('.agent-report-reason')).toBeNull();

    fireEvent.click(within(bodies[0] as HTMLElement).getByText('Open in the agent pane'));
    expect(useDockStore.getState().panels.map((p) => [p.id, p.title])).toEqual([['agent:s1', 'Scan']]);
  });

  it('keeps New on the team line until every agent report is opened', () => {
    const { container } = render(<AgentReportCard report={team()} />);
    const head = lines(container)[0];
    fireEvent.click(head);
    const agentLines = lines(container.querySelector('.agent-report-team') as HTMLElement);
    expect(agentLines.every((l) => l.querySelector('.agent-report-new'))).toBe(true);

    fireEvent.click(agentLines[0]);
    fireEvent.click(agentLines[1]);
    expect(agentLines[0].querySelector('.agent-report-new')).toBeNull();
    expect(head.querySelector('.agent-report-new')).not.toBeNull();

    fireEvent.click(agentLines[2]);
    expect(head.querySelector('.agent-report-new')).toBeNull();
    expect(head.classList.contains('unseen')).toBe(false);
  });

  it('says all finished in one done chip', () => {
    const done = report({ agents: [agent({ name: 'A', agent_id: 'x' }), agent({ name: 'B', agent_id: 'y' })] });
    const { container } = render(<AgentReportCard report={done} />);
    const chips = lines(container)[0].querySelectorAll('.activity-chip:not(.agent-report-new)');
    expect([...chips].map((c) => [c.textContent, c.className])).toEqual([['2 finished', 'activity-chip done']]);
  });
});

describe('AgentReportCard in the chat', () => {
  const row = (payload: AgentReportPayload): ChatMessageType => ({
    role: 'agent_report',
    content: '',
    timestamp: payload.timestamp ?? '',
    agentReport: payload,
  });

  it('renders at an assistant turn width, with no message actions', () => {
    const { container } = render(<ChatMessage message={row(report({ timestamp: '2026-10-19T09:30:00Z' }))} index={3} />);
    const wrap = container.firstElementChild as HTMLElement;
    expect(wrap.matches('.chat-msg-wrap.assistant-wrap')).toBe(true);
    const card = wrap.querySelector('[data-testid="agent-report-card"]');
    expect(card?.parentElement).toBe(wrap);
    expect(wrap.querySelector('.chat-msg')).toBeNull();
    expect(wrap.querySelector('.msg-actions')).toBeNull();
    const time = wrap.querySelector('.msg-timestamp');
    expect(time).not.toBeNull();
    expect(card!.compareDocumentPosition(time!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('shows no timestamp when the payload has none', () => {
    const { container } = render(<ChatMessage message={row(report())} index={3} />);
    expect(container.querySelector('.msg-timestamp')).toBeNull();
  });

  it('draws nothing for a report with no agents', () => {
    const { container } = render(<ChatMessage message={row(report({ timestamp: '2026-10-19T09:30:00Z', agents: [] }))} index={3} />);
    expect(container.innerHTML).toBe('');
  });
});
