/**
 * A model-written caption for calls that are still live shows on their live
 * activity row the moment its activity_label frame lands, not only once the
 * row commits. The commit puts the same caption on the committed row, and
 * the live copy clears with the live rows, so the caption is never shown
 * twice. A Stop commits it the same way. Real frames through readSSEStream,
 * sent one at a time.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { render, waitFor } from '@testing-library/react';
import { StreamingMessage } from './StreamingMessage';
import { TurnBubble } from './TurnBubble';
import { readSSEStream } from '@/hooks/chatStream/sseStream';
import { killSessionStream, registerStreamController } from '@/hooks/chatStream/streamControl';
import { dropRuntime, getChatStore } from '@/stores/sessionRuntimes';
import { useSessionStore } from '@/stores/sessionStore';
import type { TurnModelSettings } from '@/types/chat';

const SETTINGS: TurnModelSettings = {
  model: 'test-model',
  effort_level: 'normal',
  verbosity: 'medium',
  brief_mode: false,
};

/** An SSE response whose frames the test sends one at a time. */
function liveResponse() {
  const enc = new TextEncoder();
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({ start(c) { controller = c; } });
  return {
    response: new Response(stream, { status: 200, headers: { 'Content-Type': 'text/event-stream' } }),
    send: (...frames: unknown[]) => {
      for (const f of frames) controller.enqueue(enc.encode(`data: ${JSON.stringify(f)}\n\n`));
    },
    end: () => {
      controller.enqueue(enc.encode('data: [DONE]\n\n'));
      controller.close();
    },
  };
}

const call = (id: string, name = 'ws_read_file') => [
  { skill: name, tool_use_id: id },
  { skill_input: name, input: { path: `${id}.ts` }, tool_use_id: id },
  { skill_result: name, output: 'ok', tool_use_id: id, is_error: false, exit_code: null },
];
const CAPTION = 'Found the config loader';

describe('StreamingMessage live captions', () => {
  const sid = 'sess-live-caption';
  afterEach(() => {
    vi.unstubAllGlobals();
    dropRuntime(sid);
    useSessionStore.setState({ currentSessionId: null });
  });

  it('shows a caption on the live row as it lands, and the same caption on the committed row', async () => {
    useSessionStore.setState({ currentSessionId: sid });
    const chat = getChatStore(sid);
    chat.getState().setStreaming(true);
    const live = liveResponse();
    const reading = readSSEStream(live.response, sid, new AbortController().signal, SETTINGS);
    const view = render(<StreamingMessage content="" isStreaming />);
    const liveLabel = () => view.container.querySelector('.activity-row .activity-label')?.textContent;

    live.send(...call('tu_1'), ...call('tu_2', 'ws_grep'));
    await waitFor(() => expect(view.container.querySelectorAll('.activity-row')).toHaveLength(1));
    const deterministic = liveLabel();
    expect(deterministic).toBeTruthy();
    expect(deterministic).not.toBe(CAPTION);

    // The caption names the round's calls while they are still live.
    live.send({ activity_label: { tool_use_id: 'tu_1', tool_use_ids: ['tu_1', 'tu_2'], label: CAPTION } });
    await waitFor(() => expect(liveLabel()).toBe(CAPTION));
    expect(chat.getState().messages).toEqual([]);

    // A card commits the segment: the committed row carries the caption, and
    // the live rows and their caption are gone.
    live.send({ workflow_started: { run_id: 'wf_1', name: 'nightly' } });
    live.end();
    const result = await reading;
    await waitFor(() => expect(view.container.querySelector('.activity-row')).toBeNull());
    expect(chat.getState().currentStreamLabels).toEqual([]);
    expect(result.activityLabels).toEqual([]);

    const [segment] = chat.getState().messages;
    expect(segment.activityLabels).toEqual({ tu_1: CAPTION });
    const committed = render(<TurnBubble rows={[segment]} indices={[0]} />);
    const rows = committed.container.querySelectorAll('.activity-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].querySelector('.activity-label')?.textContent).toBe(CAPTION);
  });

  it('a Stop keeps the caption the live row showed on the (Stopped) row', async () => {
    // The kill switch also asks the backend to stop this turn's shell work.
    vi.stubGlobal('fetch', vi.fn().mockResolvedValue({ ok: true } as Response));
    useSessionStore.setState({ currentSessionId: sid });
    const chat = getChatStore(sid);
    chat.getState().setStreaming(true);
    const controller = new AbortController();
    registerStreamController(sid, controller, { startsTurn: true });
    const live = liveResponse();
    const reading = readSSEStream(live.response, sid, controller.signal, SETTINGS);
    const view = render(<StreamingMessage content="" isStreaming />);
    const liveLabel = () => view.container.querySelector('.activity-row .activity-label')?.textContent;

    // tu_2 is still running when the caption lands and when the user stops.
    live.send(...call('tu_1'), ...call('tu_2', 'ws_grep').slice(0, 2));
    await waitFor(() => expect(view.container.querySelectorAll('.activity-row')).toHaveLength(1));
    live.send({ activity_label: { tool_use_id: 'tu_1', tool_use_ids: ['tu_1', 'tu_2'], label: CAPTION } });
    await waitFor(() => expect(liveLabel()).toBe(CAPTION));

    killSessionStream(sid);
    live.end();
    await reading;
    expect(chat.getState().currentStreamLabels).toEqual([]);

    const [stopped] = chat.getState().messages;
    expect(stopped.stopped).toBe(true);
    expect(stopped.activityLabels).toEqual({ tu_1: CAPTION });
    const committed = render(<TurnBubble rows={[stopped]} indices={[0]} />);
    const rows = committed.container.querySelectorAll('.activity-row');
    expect(rows).toHaveLength(1);
    expect(rows[0].querySelector('.activity-label')?.textContent).toBe(CAPTION);
    // The deterministic sentence is the tail, and it says the group stopped.
    expect(rows[0].querySelector('.activity-tail')?.textContent).toMatch(/stopped$/);
  });

  it('a later caption for the same live group replaces the earlier one, and the end of the stream clears them', () => {
    useSessionStore.setState({ currentSessionId: sid });
    const chat = getChatStore(sid).getState();
    chat.setStreaming(true);
    for (const id of ['tu_1', 'tu_2']) {
      chat.addStreamToolUse({ toolId: id, toolName: 'ws_read_file', input: { path: `${id}.ts` }, status: 'complete', result: 'ok' });
    }
    chat.addStreamLabel({ ids: ['tu_1'], label: 'Read the plan' });
    chat.addStreamLabel({ ids: ['tu_2'], label: 'Read the plan and its tests' });
    chat.addStreamLabel({ ids: ['tu_elsewhere'], label: 'Not this group' });
    const view = render(<StreamingMessage content="" isStreaming />);
    expect(view.container.querySelector('.activity-label')?.textContent).toBe('Read the plan and its tests');

    getChatStore(sid).getState().finishStream();
    expect(getChatStore(sid).getState().currentStreamLabels).toEqual([]);
  });
});
