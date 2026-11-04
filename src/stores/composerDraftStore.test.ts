import { beforeEach, describe, expect, it } from 'vitest';
import { renderHook, act } from '@testing-library/react';
import { useComposerDraftStore } from './composerDraftStore';
import { useComposerDraft } from '@/hooks/useComposerDraft';
import { DRAFT_SESSION } from './sessionRuntimes';
import { useSessionStore } from './sessionStore';

const STORAGE_KEY = 'whisper_composer_drafts';

beforeEach(() => {
  localStorage.clear();
  useComposerDraftStore.setState({ bySession: {} });
});

describe('composer drafts', () => {
  it('keep each session its own text', () => {
    const { result, rerender } = renderHook(({ id }) => useComposerDraft(id), {
      initialProps: { id: 'session-a' as string | null },
    });
    act(() => result.current[1]('Ask about @Weekly sync notes'));

    rerender({ id: 'session-b' });
    expect(result.current[0]).toBe('');
    act(() => result.current[1]((prev) => `${prev}Second draft`));

    rerender({ id: 'session-a' });
    expect(result.current[0]).toBe('Ask about @Weekly sync notes');
    rerender({ id: 'session-b' });
    expect(result.current[0]).toBe('Second draft');
  });

  it('keeps the welcome screen draft under the draft key', () => {
    const { result } = renderHook(() => useComposerDraft(null));
    act(() => result.current[1]('Welcome screen draft'));
    expect(useComposerDraftStore.getState().bySession[DRAFT_SESSION]).toBe('Welcome screen draft');
  });

  it('saves drafts so they survive a reload, and forgets an emptied one', () => {
    const { update } = useComposerDraftStore.getState();
    update('session-a', 'Keep me');
    update('session-b', 'Gone soon');
    update('session-b', '');
    expect(JSON.parse(localStorage.getItem(STORAGE_KEY) ?? '{}')).toEqual({ 'session-a': 'Keep me' });
  });

  it('moves a welcome screen draft into the session created next', () => {
    useComposerDraftStore.getState().update(DRAFT_SESSION, 'Typed before a session existed');
    const id = useSessionStore.getState().createSession();
    const drafts = useComposerDraftStore.getState().bySession;
    expect(drafts[id]).toBe('Typed before a session existed');
    expect(drafts[DRAFT_SESSION]).toBeUndefined();
  });

  it('drops a deleted session draft', () => {
    useComposerDraftStore.getState().update('session-a', 'Draft');
    useComposerDraftStore.getState().clearSession('session-a');
    expect(useComposerDraftStore.getState().bySession).toEqual({});
  });
});
