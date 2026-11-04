import { useCallback } from 'react';
import { useComposerDraftStore } from '@/stores/composerDraftStore';
import { DRAFT_SESSION } from '@/stores/sessionRuntimes';

/**
 * The composer text for one session, with the exact React setState
 * signature (a value or a functional updater). A null session (the welcome
 * screen, before the first turn) uses the draft key. Submit clears the text
 * before chatStream.send creates the real session, so a sent message never
 * comes back as a draft.
 */
export function useComposerDraft(
  sessionId: string | null,
): [string, React.Dispatch<React.SetStateAction<string>>] {
  const key = sessionId ?? DRAFT_SESSION;
  const text = useComposerDraftStore((s) => s.bySession[key] ?? '');
  const update = useComposerDraftStore((s) => s.update);
  const setText = useCallback<React.Dispatch<React.SetStateAction<string>>>(
    (value) => update(key, value),
    [update, key],
  );
  return [text, setText];
}
