import { beforeEach, describe, expect, it, vi } from 'vitest';
import { applyVoiceEvent, mergeSpoken, stepsToToolUse, type VoiceEventDeps } from './voiceEvents';
import type { ChatMessage } from '@/types/chat';
import { useVoiceStore } from '@/stores/voiceStore';
import { VoiceServerEventSchema, type VoiceServerEvent } from '@/types/schemas/voice.schema';

function deps(): VoiceEventDeps & { calls: Record<string, unknown[][]> } {
  const calls: Record<string, unknown[][]> = {};
  const rec = (name: string) => (...args: unknown[]) => {
    (calls[name] ??= []).push(args);
  };
  return {
    calls,
    commitUser: rec('commitUser'),
    commitAssistant: rec('commitAssistant'),
    flushAudio: rec('flushAudio'),
    onClientAction: rec('onClientAction'),
    onEnded: rec('onEnded'),
    onDraining: rec('onDraining'),
    toast: rec('toast'),
  };
}

const ev = (raw: unknown): VoiceServerEvent => VoiceServerEventSchema.parse(raw);

describe('applyVoiceEvent', () => {
  beforeEach(() => {
    useVoiceStore.getState().reset();
    useVoiceStore.getState().begin('s1');
  });

  it('ready + state drive the status, but never resurrect an ended session', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'ready', model_id: 'amazon.nova-2-sonic-v1:0', voice_id: 'amy' }), d);
    expect(useVoiceStore.getState().status).toBe('listening');
    expect(useVoiceStore.getState().voiceId).toBe('amy');
    expect(useVoiceStore.getState().streamStartedAt).not.toBeNull();
    applyVoiceEvent(ev({ type: 'state', state: 'speaking' }), d);
    expect(useVoiceStore.getState().status).toBe('speaking');
    useVoiceStore.getState().setStatus('ending');
    applyVoiceEvent(ev({ type: 'state', state: 'listening' }), d);
    expect(useVoiceStore.getState().status).toBe('ending');
  });

  it('spoken and typed user turns are committed with the typed flag', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'user_transcript', text: 'Run the tests.' }), d);
    applyVoiceEvent(ev({ type: 'user_transcript', text: 'and commit', typed: true }), d);
    expect(d.calls.commitUser).toEqual([['Run the tests.', false], ['and commit', true]]);
  });

  it('speculative text accumulates live; the final text commits a spoken bubble', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'Sure, ', final: false }), d);
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'on it.', final: false }), d);
    expect(useVoiceStore.getState().liveText).toBe('Sure, on it.');
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'Sure, on it.', final: true }), d);
    expect(d.calls.commitAssistant).toEqual([['Sure, on it.', [], true]]);
    expect(useVoiceStore.getState().liveText).toBe('');
  });

  it("spoken bubbles never carry the delegated run's steps; the run keeps them for its written answer", () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'tool_call', tool_use_id: 'tu1', name: 'ask_assistant', input: { request: 'x' } }), d);
    applyVoiceEvent(ev({ type: 'assistant_step', name: 'ws_open_folder', status: 'running', detail: 'p' }), d);
    // Sonic fills the silence; its bubble must not adopt ws_open_folder.
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'Working on it.', final: true }), d);
    expect(d.calls.commitAssistant).toEqual([['Working on it.', [], true]]);
    expect(useVoiceStore.getState().steps.map((s) => s.name)).toEqual(['ws_open_folder']);
    applyVoiceEvent(ev({ type: 'assistant_step', name: 'ws_open_folder', status: 'ok', detail: 'opened' }), d);
    applyVoiceEvent(ev({ type: 'assistant_answer', run_id: 'r1', request: 'x', output: 'Opened it.', status: 'ok' }), d);
    applyVoiceEvent(ev({ type: 'tool_result', tool_use_id: 'tu1', name: 'ask_assistant', output: 'Opened it.', status: 'ok' }), d);
    expect(d.calls.commitAssistant).toHaveLength(2);
    const [, tools, spoken] = d.calls.commitAssistant[1] as [string, ReturnType<typeof stepsToToolUse>, boolean];
    expect(spoken).toBe(false);
    expect(tools.map((t) => [t.toolName, t.status])).toEqual([['ws_open_folder', 'complete']]);
  });

  it('a paused delegate result shows no bubble and keeps the steps for the resumed run', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'tool_call', tool_use_id: 'tu1', name: 'ask_assistant', input: { request: 'open x' } }), d);
    applyVoiceEvent(ev({ type: 'assistant_step', name: 'ws_open_folder', status: 'running', detail: 'p' }), d);
    applyVoiceEvent(
      ev({ type: 'tool_result', tool_use_id: 'tu1', name: 'ask_assistant', output: 'The assistant paused: needs approval.', status: 'paused' }),
      d,
    );
    expect(d.calls.commitAssistant).toBeUndefined();
    expect(useVoiceStore.getState().steps.some((s) => s.name === 'ws_open_folder')).toBe(true);
  });

  it("the delegated assistant's answer lands as a written bubble with its steps; Sonic's summary stays spoken", () => {
    const d = deps();
    applyVoiceEvent(
      ev({ type: 'tool_call', tool_use_id: 'tu1', name: 'ask_assistant', input: { request: 'list branches' } }),
      d,
    );
    expect(useVoiceStore.getState().pendingTools).toBe(1);
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'r1', name: 'git_branch_list', status: 'running', detail: '{}' }), d);
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'r1', name: 'git_branch_list', status: 'ok', detail: 'main, sonic' }), d);
    applyVoiceEvent(
      ev({ type: 'assistant_answer', run_id: 'r1', request: 'list branches', output: 'Branches: main, sonic', status: 'ok' }),
      d,
    );
    applyVoiceEvent(
      ev({ type: 'tool_result', tool_use_id: 'tu1', name: 'ask_assistant', output: 'Branches: main, sonic', status: 'ok' }),
      d,
    );
    expect(useVoiceStore.getState().pendingTools).toBe(0);
    expect(d.calls.commitAssistant).toHaveLength(1);
    const [text, tools, spoken] = d.calls.commitAssistant[0] as [string, ReturnType<typeof stepsToToolUse>, boolean];
    expect(text).toBe('Branches: main, sonic');
    expect(spoken).toBe(false);
    // Only Claude's inner steps ride along, not the ask_assistant call itself.
    expect(tools.map((t) => [t.toolName, t.status, t.result])).toEqual([['git_branch_list', 'complete', 'main, sonic']]);
    expect(useVoiceStore.getState().steps).toEqual([]);
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'There are two branches.', final: true }), d);
    expect(d.calls.commitAssistant[1]).toEqual(['There are two branches.', [], true]);
  });

  it('a background run keeps its steps on screen while other requests are served, then lands its own answer', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'tool_call', tool_use_id: 'tu1', name: 'ask_assistant', input: { request: 'check for bugs' } }), d);
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'bugs', name: 'spawn_agent', status: 'running', detail: 'explore' }), d);
    // Past the wait: ask_assistant reports "working"; nothing is discarded.
    applyVoiceEvent(
      ev({ type: 'tool_result', tool_use_id: 'tu1', name: 'ask_assistant', output: 'Working on it: check for bugs.', status: 'working' }),
      d,
    );
    expect(d.calls.commitAssistant).toBeUndefined();
    expect(useVoiceStore.getState().steps.map((s) => s.name)).toEqual(['spawn_agent']);
    // A quick request served meanwhile takes only its own steps with it.
    applyVoiceEvent(ev({ type: 'tool_call', tool_use_id: 'tu2', name: 'ask_assistant', input: { request: 'check the readme' } }), d);
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'readme', name: 'ws_read_file', status: 'running', detail: 'README.md' }), d);
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'readme', name: 'ws_read_file', status: 'ok', detail: '# Whisper' }), d);
    applyVoiceEvent(ev({ type: 'assistant_answer', run_id: 'readme', request: 'check the readme', output: 'The readme is fine.', status: 'ok' }), d);
    applyVoiceEvent(ev({ type: 'tool_result', tool_use_id: 'tu2', name: 'ask_assistant', output: 'The readme is fine.', status: 'ok' }), d);
    expect(d.calls.commitAssistant).toHaveLength(1);
    const [text, tools] = d.calls.commitAssistant[0] as [string, ReturnType<typeof stepsToToolUse>, boolean];
    expect(text).toBe('The readme is fine.');
    expect(tools.map((t) => t.toolName)).toEqual(['ws_read_file']);
    // The agent run is still on screen, untouched.
    expect(useVoiceStore.getState().steps.map((s) => [s.name, s.status])).toEqual([['spawn_agent', 'running']]);
    // Minutes later its answer lands with its own steps.
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'bugs', name: 'spawn_agent', status: 'ok', detail: '3 findings' }), d);
    applyVoiceEvent(ev({ type: 'assistant_answer', run_id: 'bugs', request: 'check for bugs', output: 'Found three bugs.', status: 'ok' }), d);
    expect(d.calls.commitAssistant).toHaveLength(2);
    const [late, lateTools] = d.calls.commitAssistant[1] as [string, ReturnType<typeof stepsToToolUse>, boolean];
    expect(late).toBe('Found three bugs.');
    expect(lateTools.map((t) => [t.toolName, t.status])).toEqual([['spawn_agent', 'complete']]);
    expect(useVoiceStore.getState().steps).toEqual([]);
  });

  it('one final per utterance replaces the preview; the next utterance starts its own preview', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'The README says', final: false, utterance_id: 'u1' }), d);
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'this is a Mac app.', final: false, utterance_id: 'u1' }), d);
    expect(useVoiceStore.getState().liveText).toBe('The README says this is a Mac app.');
    // Sonic moves on before the previous commit lands: the preview is replaced, not appended.
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'Anything else?', final: false, utterance_id: 'u2' }), d);
    expect(useVoiceStore.getState().liveText).toBe('Anything else?');
    // The late commit of u1 must not wipe u2's preview.
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'The README says this is a Mac app.', final: true, utterance_id: 'u1' }), d);
    expect(d.calls.commitAssistant).toEqual([['The README says this is a Mac app.', [], true]]);
    expect(useVoiceStore.getState().liveText).toBe('Anything else?');
    applyVoiceEvent(ev({ type: 'assistant_text', text: 'Anything else?', final: true, utterance_id: 'u2' }), d);
    expect(useVoiceStore.getState().liveText).toBe('');
    expect(d.calls.commitAssistant).toHaveLength(2);
  });

  it("agent progress folds into the run's reports and rides on its written answer", () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'tool_call', tool_use_id: 'tu1', name: 'ask_assistant', input: { request: 'bugs' } }), d);
    applyVoiceEvent(
      ev({ type: 'assistant_step', run_id: 'r1', name: 'spawn_agent', status: 'running', detail: 'x', input: { task: 'find bugs', agent_type: 'explorer' } }),
      d,
    );
    applyVoiceEvent(
      ev({ type: 'team_progress', run_id: 'r1', event: { phase: 'team_started', team_id: 't1', team_name: 'find bugs', agents: [{ name: 'a', task: 'find bugs', agent_type: 'explorer' }] } }),
      d,
    );
    applyVoiceEvent(
      ev({ type: 'team_progress', run_id: 'r1', event: { phase: 'tool_call', team_id: 't1', agent_id: 'a1', agent_name: 'a', tool_name: 'ws_grep', tool_input_preview: 'def' } }),
      d,
    );
    const st = useVoiceStore.getState();
    expect(Object.keys(st.teamReports)).toEqual(['t1']);
    expect(st.teamRuns).toEqual({ t1: 'r1' });
    expect(st.steps.find((x) => x.name === 'spawn_agent')?.input).toEqual({ task: 'find bugs', agent_type: 'explorer' });
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'r1', name: 'spawn_agent', status: 'ok', detail: 'done', output: '{"team_id":"t1"}' }), d);
    applyVoiceEvent(ev({ type: 'assistant_answer', run_id: 'r1', request: 'bugs', output: 'Found none.', status: 'ok' }), d);
    const call = d.calls.commitAssistant[0] as [string, ReturnType<typeof stepsToToolUse>, boolean, Record<string, unknown>];
    expect(call[0]).toBe('Found none.');
    expect(call[1].map((t) => [t.toolName, t.result])).toEqual([['spawn_agent', '{"team_id":"t1"}']]);
    expect(Object.keys(call[3])).toEqual(['t1']);
    expect(useVoiceStore.getState().teamReports).toEqual({});
  });

  it('draining hands the socket over instead of ending, and reset keeps the runs on screen', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'r1', name: 'spawn_agent', status: 'running', detail: 'x' }), d);
    applyVoiceEvent(ev({ type: 'draining', runs: [{ run_id: 'r1', request: 'bugs' }] }), d);
    expect(d.calls.onDraining).toEqual([[[{ run_id: 'r1', request: 'bugs' }]]]);
    expect(d.calls.onEnded).toBeUndefined();
    useVoiceStore.getState().adjustDraining(1);
    useVoiceStore.getState().reset();
    expect(useVoiceStore.getState().steps.map((s) => s.name)).toEqual(['spawn_agent']);
    useVoiceStore.getState().begin('s2');
    expect(useVoiceStore.getState().steps.map((s) => s.name)).toEqual(['spawn_agent']);
    useVoiceStore.getState().adjustDraining(-1);
    useVoiceStore.getState().reset();
    expect(useVoiceStore.getState().steps).toEqual([]);
  });

  it('a stopped run commits its trace with unfinished steps marked stopped, and clears its leftovers', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'r1', name: 'ws_grep', status: 'running', detail: 'TODO' }), d);
    applyVoiceEvent(ev({ type: 'assistant_step', run_id: 'r1', name: 'ws_read_file', status: 'ok', detail: 'x' }), d);
    applyVoiceEvent(
      ev({ type: 'assistant_request', kind: 'user_question', run_id: 'r1', tool_use_id: 'q', action: '', category: '', summary: 'Which?', question: 'Which?', options: ['a'], detail: '' }),
      d,
    );
    applyVoiceEvent(ev({ type: 'assistant_answer', run_id: 'r1', request: 'x', output: '(Stopped)', status: 'stopped' }), d);
    const [text, tools] = d.calls.commitAssistant[0] as [string, ReturnType<typeof stepsToToolUse>, boolean];
    expect(text).toBe('*(Stopped)*');
    expect(tools.map((t) => [t.toolName, t.status, t.result])).toEqual([
      ['ws_grep', 'stopped', '[Stopped by user]'],
      ['ws_read_file', 'complete', 'x'],
    ]);
    expect(useVoiceStore.getState().steps).toEqual([]);
    expect(useVoiceStore.getState().pendingRequest).toBeNull();
  });

  it('a pending request from the assistant is shown until resolved', () => {
    const d = deps();
    applyVoiceEvent(
      ev({
        type: 'assistant_request',
        kind: 'approval_request',
        tool_use_id: 'tu9',
        action: 'command',
        category: 'cli',
        summary: 'Run: pytest -q',
        question: '',
        options: [],
        risk_hint: 'medium',
        detail: 'pytest -q',
      }),
      d,
    );
    const pending = useVoiceStore.getState().pendingRequest;
    expect(pending?.kind).toBe('approval_request');
    expect(pending?.summary).toBe('Run: pytest -q');
    expect(pending?.riskHint).toBe('medium');
    applyVoiceEvent(ev({ type: 'assistant_request_resolved', tool_use_id: 'tu9', decision: 'approve' }), d);
    expect(useVoiceStore.getState().pendingRequest).toBeNull();
  });

  it('interrupted flushes playback; client actions, usage, errors and ended are forwarded', () => {
    const d = deps();
    applyVoiceEvent(ev({ type: 'interrupted' }), d);
    expect(d.calls.flushAudio).toHaveLength(1);
    applyVoiceEvent(ev({ type: 'client_action', action: 'recording', value: 'start' }), d);
    expect(d.calls.onClientAction).toEqual([['recording', 'start']]);
    applyVoiceEvent(
      ev({ type: 'usage', input_speech: 10, input_text: 2, output_speech: 20, output_text: 3, total_tokens: 35 }),
      d,
    );
    expect(useVoiceStore.getState().usage?.totalTokens).toBe(35);
    applyVoiceEvent(ev({ type: 'error', message: 'model access denied' }), d);
    expect(useVoiceStore.getState().error).toBe('model access denied');
    expect(d.calls.toast).toEqual([['error', 'model access denied']]);
    applyVoiceEvent(ev({ type: 'ended', reason: 'user' }), d);
    expect(d.calls.onEnded).toEqual([['user']]);
  });

  it('renewed restarts the stream clock', () => {
    const d = deps();
    useVoiceStore.setState({ streamStartedAt: 1 });
    vi.useFakeTimers();
    vi.setSystemTime(new Date('2026-09-08T12:00:00Z'));
    applyVoiceEvent(ev({ type: 'renewed' }), d);
    expect(useVoiceStore.getState().streamStartedAt).toBe(Date.now());
    vi.useRealTimers();
  });

  it('rejects malformed events at the schema boundary', () => {
    expect(VoiceServerEventSchema.safeParse({ type: 'state', state: 'dancing' }).success).toBe(false);
    expect(VoiceServerEventSchema.safeParse({ type: 'nope' }).success).toBe(false);
  });
});

describe('mergeSpoken', () => {
  const at = (iso: string) => Date.parse(iso);
  const msg = (partial: Partial<ChatMessage> & Pick<ChatMessage, 'role' | 'content'>): ChatMessage => ({
    timestamp: '2026-09-08T12:00:00.000Z',
    ...partial,
  });

  it('joins consecutive spoken pieces of the same role into one bubble', () => {
    const history = [msg({ role: 'user', content: 'just run', spoken: true })];
    const next = mergeSpoken(history, msg({ role: 'user', content: 'git branch', spoken: true }), at('2026-09-08T12:00:05.000Z'));
    expect(next).toHaveLength(1);
    expect(next?.[0].content).toBe('just run git branch');
  });

  it('never merges across roles, typed messages, written answers, or after a long gap', () => {
    const spokenUser = msg({ role: 'user', content: 'a', spoken: true });
    expect(mergeSpoken([spokenUser], msg({ role: 'assistant', content: 'b', spoken: true }))).toBeNull();
    // The server commits each assistant utterance whole: two utterances stay two bubbles.
    const spokenAssistant = msg({ role: 'assistant', content: 'First utterance.', spoken: true });
    expect(
      mergeSpoken([spokenAssistant], msg({ role: 'assistant', content: 'Second utterance.', spoken: true }), at('2026-09-08T12:00:05.000Z')),
    ).toBeNull();
    expect(mergeSpoken([spokenUser], msg({ role: 'user', content: 'typed' }))).toBeNull();
    const written = msg({ role: 'assistant', content: 'Branches: main', spoken: false });
    expect(mergeSpoken([written], msg({ role: 'assistant', content: 'Two branches.', spoken: true }))).toBeNull();
    expect(
      mergeSpoken([spokenUser], msg({ role: 'user', content: 'later', spoken: true }), at('2026-09-08T12:10:00.000Z')),
    ).toBeNull();
  });

  it('unions tool traces without duplicates', () => {
    const first = msg({
      role: 'user',
      content: 'On it.',
      spoken: true,
      toolUse: [{ toolId: 'a', toolName: 'x', input: {}, status: 'complete' }],
    });
    const incoming = msg({
      role: 'user',
      content: 'Done.',
      spoken: true,
      toolUse: [
        { toolId: 'a', toolName: 'x', input: {}, status: 'complete' },
        { toolId: 'b', toolName: 'y', input: {}, status: 'complete' },
      ],
    });
    const next = mergeSpoken([first], incoming, at('2026-09-08T12:00:30.000Z'));
    expect(next?.[0].content).toBe('On it. Done.');
    expect(next?.[0].toolUse?.map((t) => t.toolId)).toEqual(['a', 'b']);
  });
});
