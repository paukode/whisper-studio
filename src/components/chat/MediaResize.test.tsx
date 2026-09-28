/**
 * Drag-to-resize for chat pictures: the size a drag asks for, the drag's
 * commit rules, the message widening to hold a picture wider than its text
 * column, and the size saved on the message in the active session.
 *
 * jsdom lays nothing out, so the card's box is stubbed where a drag needs it.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render } from '@testing-library/react';
import { draggedSize, MIN_H, MIN_W } from './MediaResize';
import { VizCard } from './VizCard';
import { ChatMessage } from './ChatMessage';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import type { ChatMessage as ChatMessageType, ToolUseEvent, VizArtifact } from '@/types/chat';

const diagram: VizArtifact = {
  kind: 'svg',
  title: 'Flow',
  description: '',
  source: '<svg viewBox="0 0 680 100"><rect class="box"/></svg>',
};

function stubBox(el: Element, box: { left: number; width: number; height: number }) {
  vi.spyOn(el, 'getBoundingClientRect').mockReturnValue({
    x: box.left,
    y: 0,
    left: box.left,
    top: 0,
    width: box.width,
    height: box.height,
    right: box.left + box.width,
    bottom: box.height,
    toJSON: () => ({}),
  } as DOMRect);
}

function drag(grip: Element, dx: number, dy: number) {
  fireEvent.pointerDown(grip, { button: 0, clientX: 500, clientY: 400, pointerId: 1 });
  fireEvent.pointerMove(window, { clientX: 500 + dx, clientY: 400 + dy, pointerId: 1 });
  fireEvent.pointerUp(window, { pointerId: 1 });
}

afterEach(() => {
  vi.restoreAllMocks();
  getActiveChatStore().getState().setMessages([]);
});

describe('draggedSize', () => {
  const start = { w: 400, h: 300 };

  it("keeps a picture's ratio, tracking the axis the pointer moved further", () => {
    // Width 400 and height 300: +100 across is a quarter, +150 down a half.
    expect(draggedSize(start, 100, 0, true, Infinity)).toEqual({ w: 500 });
    expect(draggedSize(start, 0, 150, true, Infinity)).toEqual({ w: 600 });
    expect(draggedSize(start, 100, 150, true, Infinity)).toEqual({ w: 600 });
    // It shrinks from either axis alone, too.
    expect(draggedSize(start, -100, 0, true, Infinity)).toEqual({ w: 300 });
    expect(draggedSize(start, -100, -150, true, Infinity)).toEqual({ w: 200 });
    // On the picture's own diagonal both axes agree, so crossing it from one
    // side to the other changes the size by no more than a pixel.
    expect(draggedSize(start, 40, 30, true, Infinity)).toEqual({ w: 440 });
    const below = draggedSize(start, 40, 31, true, Infinity).w;
    const beside = draggedSize(start, 41, 30, true, Infinity).w;
    expect(Math.abs(below - beside)).toBeLessThanOrEqual(1);
  });

  it('gives a chart the width and the height the pointer asks for', () => {
    expect(draggedSize(start, 50, 80, false, Infinity)).toEqual({ w: 450, h: 380 });
  });

  it('never goes under the minimum or past the room there is', () => {
    expect(draggedSize(start, -1000, 0, true, Infinity).w).toBe(MIN_W);
    expect(draggedSize(start, 5000, 0, true, 900).w).toBe(900);
    expect(draggedSize(start, 0, -1000, false, Infinity).h).toBe(MIN_H);
  });
});

describe('VizCard resizing', () => {
  it('has no grip when nothing can save a size', () => {
    const { container } = render(<VizCard viz={diagram} />);
    expect(container.querySelector('.media-grip')).toBeNull();
  });

  it('saves once when a drag ends, nothing for a click, and resets on a double click', () => {
    const onResize = vi.fn();
    const { container } = render(<VizCard viz={diagram} onResize={onResize} />);
    const card = container.querySelector('.viz-card')!;
    stubBox(card, { left: 100, width: 400, height: 300 });
    const grip = container.querySelector('.media-grip')!;

    fireEvent.pointerDown(grip, { button: 0, clientX: 500, clientY: 400, pointerId: 1 });
    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(onResize).not.toHaveBeenCalled();

    fireEvent.pointerDown(grip, { button: 0, clientX: 500, clientY: 400, pointerId: 1 });
    fireEvent.pointerMove(window, { clientX: 560, clientY: 400, pointerId: 1 });
    // The card follows the pointer while it moves; nothing is saved yet.
    expect(card).toHaveClass('is-resizing');
    expect((card as HTMLElement).style.getPropertyValue('--media-w')).toBe('460px');
    expect(onResize).not.toHaveBeenCalled();
    fireEvent.pointerMove(window, { clientX: 600, clientY: 400, pointerId: 1 });
    fireEvent.pointerUp(window, { pointerId: 1 });
    expect(onResize).toHaveBeenCalledTimes(1);
    expect(onResize).toHaveBeenCalledWith({ w: 500 });
    expect(card).not.toHaveClass('is-resizing');

    fireEvent.doubleClick(grip);
    expect(onResize).toHaveBeenLastCalledWith(null);
  });

  it('widens its message to the widest sized picture, and lets go when reset', () => {
    const inMessage = (sizes: Array<number | undefined>) => (
      <div className="chat-messages">
        <div className="chat-msg-wrap assistant-wrap">
          <div className="chat-msg assistant">
            {sizes.map((w, i) => (
              <VizCard key={i} viz={diagram} size={w ? { w } : undefined} onResize={vi.fn()} />
            ))}
          </div>
        </div>
      </div>
    );
    const { container, rerender } = render(inMessage([900, 600]));
    const wrap = container.querySelector<HTMLElement>('.chat-msg-wrap')!;
    const cards = container.querySelectorAll('.viz-card');
    expect(cards[0]).toHaveClass('media-sized');
    expect((cards[0] as HTMLElement).style.getPropertyValue('--media-w')).toBe('900px');
    // Never wider than the chat, whatever the saved width.
    expect(wrap.style.minWidth).toBe('min(100%, 900px)');

    rerender(inMessage([undefined, 600]));
    expect(cards[0]).not.toHaveClass('media-sized');
    expect(wrap.style.minWidth).toBe('min(100%, 600px)');

    rerender(inMessage([undefined, undefined]));
    expect(wrap.style.minWidth).toBe('');
  });
});

describe('ChatMessage saves picture sizes on the message', () => {
  const assistant = (over: Partial<ChatMessageType>): ChatMessageType => ({
    role: 'assistant',
    content: 'Here it is.',
    timestamp: '2026-09-25T10:00:00.000Z',
    ...over,
  });

  it('stores a dragged diagram size under its visual index, and a reset removes it', () => {
    const message = assistant({ visuals: [diagram, diagram] });
    getActiveChatStore().getState().setMessages([message]);
    const { container } = render(<ChatMessage message={message} index={0} />);
    const second = container.querySelectorAll('.viz-card')[1];
    stubBox(second, { left: 100, width: 400, height: 300 });

    drag(second.querySelector('.media-grip')!, 100, 0);
    const saved = getActiveChatStore().getState().messages[0];
    expect(saved.mediaSizes).toEqual({ 'viz-1': { w: 500 } });
    // The rest of the message is untouched.
    expect(saved.visuals).toBe(message.visuals);

    const { container: again } = render(<ChatMessage message={saved} index={0} />);
    fireEvent.doubleClick(again.querySelectorAll('.viz-card')[1].querySelector('.media-grip')!);
    expect(getActiveChatStore().getState().messages[0].mediaSizes).toBeUndefined();
  });

  it("keys a screenshot by its step's place in toolUse", () => {
    const read: ToolUseEvent = {
      toolId: 'r1',
      toolName: 'ws_read_file',
      input: { path: '/repo/a.ts' },
      result: 'const a = 1;',
      status: 'complete',
    };
    const shot: ToolUseEvent = {
      toolId: 's1',
      toolName: 'preview_screenshot',
      input: {},
      result: 'Screenshot of the preview',
      status: 'complete',
      previewImage: { media_type: 'image/png', data: 'iVBORw0KGgo=' },
    };
    const message = assistant({ toolUse: [read, shot], mediaSizes: { 'shot-1': { w: 320 } } });
    getActiveChatStore().getState().setMessages([message]);
    const { container } = render(<ChatMessage message={message} index={0} />);
    const card = container.querySelector('.preview-screenshot-card')!;
    expect(card).toHaveClass('media-sized');

    fireEvent.doubleClick(card.querySelector('.media-grip')!);
    expect(getActiveChatStore().getState().messages[0].mediaSizes).toBeUndefined();
  });
});
