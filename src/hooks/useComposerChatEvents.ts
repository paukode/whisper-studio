import { useEffect } from 'react';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import type { useChatStream } from '@/hooks/useChatStream';

type ChatStream = ReturnType<typeof useChatStream>;

/**
 * Wire the composer to `whisper-regenerate`, which the Regenerate button of a
 * user bubble (ChatMessage) or an assistant turn (TurnBubble) dispatches,
 * naming the user message to send again. Truncates history at that index and
 * resends its content, re-attaching the original files by id so
 * regenerate/edit-resend doesn't drop them. (Cards that answer a paused turn
 * resume it through hooks/chatStream/pausedAnswer.)
 */
export function useComposerChatEvents(chatStream: ChatStream): void {
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as
        | {
            index?: number;
            content?: string;
            attachmentIds?: string[];
            attachmentNames?: string[];
          }
        | undefined;
      if (detail?.content != null && detail?.index != null) {
        getActiveChatStore().getState().deleteMessagesFrom(detail.index);
        void chatStream.send(detail.content, {
          attachmentIds: detail.attachmentIds,
          attachmentNames: detail.attachmentNames,
        });
      }
    };
    window.addEventListener('whisper-regenerate', handler);
    return () => window.removeEventListener('whisper-regenerate', handler);
  }, [chatStream]);
}
