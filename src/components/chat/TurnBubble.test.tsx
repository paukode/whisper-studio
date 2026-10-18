/**
 * One assistant turn, one bubble: the rows a turn committed (prose before a
 * tool call, the tool work, the cards) render in order inside one
 * .chat-msg.assistant, and the turn's chrome comes once: the Verified chips
 * merged over every row, the timestamp of the last row, one set of actions.
 * The actions address the store rows the turn covers, and every card still
 * answers on its own row.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { TurnBubble } from './TurnBubble';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import type { SessionTask } from '@/stores/taskStore';
import type { ChatMessage, Delivery, ToolUseEvent } from '@/types/chat';

const REPORT: Delivery = { kind: 'file', target: '/Users/me/Downloads/report.html', label: 'report.html', detail: '12.4 KB' };
const RESAVED: Delivery = { ...REPORT, detail: '13.0 KB' };
const PUSH: Delivery = { kind: 'push', target: 'main', label: 'main', detail: 'to origin/main' };

const read = (path: string, id: string): ToolUseEvent => ({
  toolId: id,
  toolName: 'ws_read_file',
  input: { path },
  result: 'const a = 1;',
  status: 'complete',
});

const at = (s: number) => `2026-10-18T14:32:${String(s).padStart(2, '0')}.000Z`;

const question: ChatMessage = { role: 'user', content: 'Make the row say what it read.', timestamp: at(0) };
const opening: ChatMessage = {
  role: 'assistant',
  content: "I'll start with the reducer.",
  timestamp: at(1),
};
const reads: ChatMessage = {
  role: 'assistant',
  content: 'The reducer commits prose on its own. Saved report.html.',
  timestamp: at(2),
  toolUse: [read('/repo/src/sseStream.ts', 'tu_1'), read('/repo/src/ActivityRow.tsx', 'tu_2')],
  deliveries: [REPORT],
};
const card: ChatMessage = {
  role: 'assistant',
  content: '',
  timestamp: at(3),
  toolUse: [{ toolId: 'goal_eval', toolName: 'goal_eval', input: { verdict: 'achieved' }, status: 'complete' }],
};
const recap: ChatMessage = {
  role: 'assistant',
  content: 'Done: pushed main.',
  timestamp: at(4),
  deliveries: [PUSH, RESAVED],
};
const next: ChatMessage = { role: 'user', content: 'Thanks.', timestamp: at(5) };

const turn = [opening, reads, card, recap];
const indices = [1, 2, 3, 4];

beforeEach(() => {
  getActiveChatStore().getState().setMessages([question, ...turn, next]);
});

afterEach(() => {
  getActiveChatStore().getState().setMessages([]);
  vi.restoreAllMocks();
});

describe('TurnBubble', () => {
  it('draws the whole turn in one bubble, with the chips, timestamp and actions once', () => {
    const { container } = render(<TurnBubble rows={turn} indices={indices} />);
    const bubbles = container.querySelectorAll('.chat-msg.assistant');
    expect(bubbles).toHaveLength(1);
    const bubble = bubbles[0];
    // The narration and the recap, in order, around the activity rows.
    const prose = [...bubble.querySelectorAll('.markdown-content')].map((p) => p.textContent?.trim());
    expect(prose).toEqual([
      "I'll start with the reducer.",
      'The reducer commits prose on its own. Saved report.html.',
      'Done: pushed main.',
    ]);
    expect(bubble.querySelectorAll('.activity-row')).toHaveLength(2);
    // One Verified row, merged by kind and target in first-seen order: the
    // resaved report replaces the first report where it stood.
    const rows = bubble.querySelectorAll('.delivery-row');
    expect(rows).toHaveLength(1);
    const chips = [...rows[0].querySelectorAll('.delivery-chip')].map((c) => c.textContent);
    expect(chips).toEqual(['report.html13.0 KB', 'mainto origin/main']);
    // The last row's timestamp, once, after the chips; one set of actions.
    expect(bubble.querySelectorAll('.msg-timestamp')).toHaveLength(1);
    expect(rows[0].compareDocumentPosition(bubble.querySelector('.msg-timestamp')!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(bubble.querySelectorAll('.msg-actions')).toHaveLength(1);
    expect(within(bubble as HTMLElement).getAllByTitle('Copy')).toHaveLength(1);
  });

  it('copies the prose of every row that has any, a blank line between them', () => {
    const writeText = vi.fn(() => Promise.resolve());
    Object.defineProperty(navigator, 'clipboard', { value: { writeText }, configurable: true });
    render(<TurnBubble rows={turn} indices={indices} />);
    fireEvent.click(screen.getByTitle('Copy'));
    expect(writeText).toHaveBeenCalledWith(
      "I'll start with the reducer.\n\nThe reducer commits prose on its own. Saved report.html.\n\nDone: pushed main.",
    );
  });

  it('deletes every row of the turn and nothing else', () => {
    render(<TurnBubble rows={turn} indices={indices} />);
    fireEvent.click(screen.getByTitle('Delete'));
    expect(getActiveChatStore().getState().messages).toEqual([question, next]);
  });

  it('regenerates from the user message the turn answers', () => {
    const regenerate = vi.fn();
    window.addEventListener('whisper-regenerate', regenerate);
    try {
      render(<TurnBubble rows={turn} indices={indices} />);
      fireEvent.click(screen.getByTitle('Regenerate'));
    } finally {
      window.removeEventListener('whisper-regenerate', regenerate);
    }
    expect(regenerate).toHaveBeenCalledTimes(1);
    const detail = (regenerate.mock.calls[0][0] as CustomEvent).detail;
    expect(detail).toMatchObject({ index: 1, content: question.content });
  });

  it('draws the live tail inside the same bubble, with the committed chips, and no timestamp or actions yet', () => {
    const renderLive = vi.fn((prior: readonly Delivery[]) => (
      <div className="live-tail">{prior.map((d) => d.label).join(',')}</div>
    ));
    const { container } = render(<TurnBubble rows={[opening, reads]} indices={[1, 2]} renderLive={renderLive} />);
    const bubble = container.querySelector('.chat-msg.assistant')!;
    const tail = bubble.querySelector('.live-tail');
    expect(tail?.textContent).toBe('report.html');
    // The tail comes after the committed rows.
    const lastProse = [...bubble.querySelectorAll('.markdown-content')].pop()!;
    expect(lastProse.compareDocumentPosition(tail!) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    // The tail carries the chips while the turn runs; the rest waits for the end.
    expect(bubble.querySelector('.delivery-row')).toBeNull();
    expect(bubble.querySelector('.msg-timestamp')).toBeNull();
    expect(bubble.querySelector('.msg-actions')).toBeNull();
  });

  it('gives each row its own task checkpoint', () => {
    const tasks = (status: string): SessionTask[] => [{ id: '1', subject: 'Write the fold', status }];
    const created: ChatMessage = {
      role: 'assistant', content: 'Planning.', timestamp: at(1),
      toolUse: [{ toolId: 'tu_c', toolName: 'task_create', input: { subject: 'Write the fold' }, status: 'complete' }],
    };
    const updated: ChatMessage = {
      role: 'assistant', content: 'Done with it.', timestamp: at(2),
      toolUse: [{ toolId: 'tu_u', toolName: 'task_update', input: { task_id: '1', status: 'completed' }, status: 'complete' }],
    };
    const { container } = render(
      <TurnBubble
        rows={[created, updated]}
        indices={[1, 2]}
        taskCheckpoints={new Map([[1, tasks('pending')], [2, tasks('completed')]])}
        liveCheckpointIdx={2}
      />,
    );
    const counts = [...container.querySelectorAll('.task-row-count')].map((c) => c.textContent);
    expect(counts).toEqual(['0/1', '1/1']);
    // Each Tasks row sits in its own row's place: the second after "Planning.".
    const taskRows = container.querySelectorAll('.task-row');
    const planning = container.querySelector('.markdown-content')!;
    expect(planning.compareDocumentPosition(taskRows[1]) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
    expect(taskRows[0].compareDocumentPosition(planning) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('answers a question card on its own row, with its own tool_use_id', () => {
    const asked: ChatMessage = {
      role: 'assistant',
      content: 'Which database should the planner use?',
      timestamp: at(2),
      userQuestions: [{ question: 'Which DB?', options: ['sqlite', 'pg'], toolUseId: 'tu_db' }],
      turnSettings: { model: 'test-model', effort_level: 'normal', verbosity: 'medium', brief_mode: false },
    };
    getActiveChatStore().getState().setMessages([question, opening, asked]);
    const submits: unknown[] = [];
    const onSubmit = (e: Event) => submits.push((e as CustomEvent).detail);
    window.addEventListener('whisper-submit-answer', onSubmit);
    try {
      render(<TurnBubble rows={[opening, asked]} indices={[1, 2]} />);
      fireEvent.click(screen.getByRole('button', { name: 'pg' }));
    } finally {
      window.removeEventListener('whisper-submit-answer', onSubmit);
    }
    expect(submits).toEqual([{ answers: [{ tool_use_id: 'tu_db', content: 'pg' }] }]);
    const stored = getActiveChatStore().getState().messages[2];
    expect(stored.userQuestions?.[0].answered).toBe(true);
    // The row kept everything else it carried, the turn's settings included.
    expect(stored.turnSettings).toBe(asked.turnSettings);
    expect(getActiveChatStore().getState().messages[1]).toBe(opening);
  });

  it('draws a call and the refusal card committed after it as one activity group named by the refusal', () => {
    const tried: ChatMessage = {
      role: 'assistant', content: '', timestamp: at(2),
      toolUse: [
        read('/repo/a.ts', 'tu_a'),
        { toolId: 'tu_rm', toolName: 'ws_run_command', input: { command: 'rm -rf build' }, status: 'error', result: 'Blocked by hook' },
      ],
    };
    const refused: ChatMessage = {
      role: 'assistant', content: '', timestamp: at(3),
      toolUse: [{ toolId: 'hook_blocked', toolName: 'hook_blocked', input: { tool_name: 'ws_run_command', reason: 'rm is not allowed' }, status: 'error' }],
    };
    const after: ChatMessage = { role: 'assistant', content: 'The hook refused it.', timestamp: at(4) };
    const { container } = render(<TurnBubble rows={[opening, tried, refused, after]} indices={[1, 2, 3, 4]} />);
    const groups = container.querySelectorAll('.activity-row');
    expect(groups).toHaveLength(1);
    expect(groups[0].querySelector('.activity-label')?.textContent).toBe('Blocked by hook');
    // One list of traces between the two paragraphs.
    expect(container.querySelectorAll('.skill-traces')).toHaveLength(1);
  });

  it('reads a group label and a Tasks checkpoint from the row that holds the call', () => {
    const tasks: SessionTask[] = [{ id: '1', subject: 'Write the fold', status: 'in_progress' }];
    const looked: ChatMessage = {
      role: 'assistant', content: '', timestamp: at(2),
      toolUse: [read('/repo/src/reducer.ts', 'tu_r')],
      activityLabels: { tu_r: 'Read the reducer' },
    };
    const planned: ChatMessage = {
      role: 'assistant', content: '', timestamp: at(3),
      toolUse: [{ toolId: 'tu_c', toolName: 'task_create', input: { subject: 'Write the fold' }, status: 'complete' }],
    };
    const { container } = render(
      <TurnBubble rows={[looked, planned]} indices={[1, 2]} taskCheckpoints={new Map([[2, tasks]])} liveCheckpointIdx={2} />,
    );
    expect(container.querySelectorAll('.skill-traces')).toHaveLength(1);
    expect(container.querySelector('.activity-label')?.textContent).toBe('Read the reducer');
    expect([...container.querySelectorAll('.task-row-count')].map((c) => c.textContent)).toEqual(['0/1']);
  });

  it('resumes a workspace prompt card with its own tool_use_id, on the row the store holds', async () => {
    const originalFetch = globalThis.fetch;
    globalThis.fetch = vi.fn((url: string) =>
      Promise.resolve({
        ok: true,
        json: async () => (String(url).includes('/api/workspace/connect') ? { writable: true } : { connected: false }),
      }),
    ) as unknown as typeof fetch;
    const paused: ChatMessage = {
      role: 'assistant', content: '', timestamp: at(2),
      toolUse: [{
        toolId: 'tu_ws', toolName: 'ws_workspace_prompt', status: 'complete',
        input: { suggested: '/Users/me/Projects/site', recent: [], tool_name: 'ws_edit_file' },
      }],
      turnSettings: { model: 'test-model', effort_level: 'normal', verbosity: 'medium', brief_mode: false },
    };
    getActiveChatStore().getState().setMessages([question, opening, paused]);
    const submits: unknown[] = [];
    const onSubmit = (e: Event) => submits.push((e as CustomEvent).detail);
    window.addEventListener('whisper-submit-answer', onSubmit);
    try {
      render(<TurnBubble rows={[opening, paused]} indices={[1, 2]} />);
      fireEvent.click(screen.getByText('/Users/me/Projects/site'));
      await waitFor(() => expect(submits).toHaveLength(1));
    } finally {
      window.removeEventListener('whisper-submit-answer', onSubmit);
      globalThis.fetch = originalFetch;
    }
    expect(submits[0]).toEqual({
      answers: [{ tool_use_id: 'tu_ws', content: expect.stringContaining('/Users/me/Projects/site') }],
    });
    // The card changed nothing on the rows: the store still holds the same
    // objects, the turn's settings included.
    const stored = getActiveChatStore().getState().messages;
    expect(stored[2]).toBe(paused);
    expect(stored[2].turnSettings).toBe(paused.turnSettings);
    expect(stored[1]).toBe(opening);
  });
});
