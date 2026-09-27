/**
 * The dictation socket's empty interim withdraws the live draft: the server
 * sends it when an utterance Parakeet drafted closes without a final (the VAD
 * discarded it). Ignored, the phantom words stayed in the composer.
 */
import { act, renderHook } from '@testing-library/react';
import { beforeEach, describe, expect, it, vi } from 'vitest';

const mic = vi.hoisted(() => ({
  onTranscript: null as ((text: string, isFinal: boolean) => void) | null,
}));
vi.mock('@/hooks/useChatInputMic', () => ({
  useChatInputMic: (opts: { onTranscript: (text: string, isFinal: boolean) => void }) => {
    mic.onTranscript = opts.onTranscript;
    return {
      isRecording: true,
      isConnecting: false,
      error: null,
      start: vi.fn(),
      stop: vi.fn(),
      toggle: vi.fn(),
    };
  },
}));

import { useDictationInput } from './useDictationInput';

function setup(initial: string) {
  let text = initial;
  const setText = vi.fn((v: string) => {
    text = v;
  });
  const textareaRef = { current: null };
  const hook = renderHook(() => useDictationInput({ text, setText, textareaRef, sessionId: 's1' }));
  hook.result.current.inputTextRef.current = initial;
  return { current: () => text };
}

describe('dictation draft withdrawal', () => {
  beforeEach(() => {
    mic.onTranscript = null;
  });

  it('puts back the text the withdrawn draft was building on', () => {
    const composer = setup('Draft so far');
    act(() => mic.onTranscript!('um', false));
    expect(composer.current()).toContain('um');
    act(() => mic.onTranscript!('', false));
    expect(composer.current()).toBe('Draft so far');
  });

  it('keeps a settled sentence and drafts the next one on top of it', () => {
    const composer = setup('');
    act(() => mic.onTranscript!('hello there', true));
    const settled = composer.current();
    act(() => mic.onTranscript!('um', false));
    act(() => mic.onTranscript!('', false));
    expect(composer.current()).toBe(settled);
    act(() => mic.onTranscript!('and more', false));
    expect(composer.current()).toContain('and more');
  });

  it('ignores an empty interim when no draft is showing', () => {
    const composer = setup('typed');
    act(() => mic.onTranscript!('', false));
    expect(composer.current()).toBe('typed');
  });
});
