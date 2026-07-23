import { useEffect } from 'react';
import { getActiveChatStore } from '@/stores/sessionRuntimes';
import type { useChatStream } from '@/hooks/useChatStream';

type ChatStream = ReturnType<typeof useChatStream>;

/**
 * Wire the composer to the two window CustomEvents other components dispatch:
 *
 *  - `whisper-submit-answer` — from UserQuestionCard / UserQuestionGroupCard.
 *    The `detail.answers` array ({tool_use_id, content}) is sent via
 *    `approvedToolResult` so Bedrock sees proper tool_results for the
 *    ask_user_question tool_use blocks. Legacy `detail.answer` (single string)
 *    falls back to a plain chat send.
 *  - `whisper-regenerate` — from ChatMessage action buttons. Truncates history
 *    at the message index and resends its content, re-attaching the original
 *    files by id so regenerate/edit-resend doesn't drop them.
 */
export function useComposerChatEvents(chatStream: ChatStream): void {
  useEffect(() => {
    const handler = (e: Event) => {
      const detail = (e as CustomEvent).detail as
        | {
            answer?: string;
            answers?: Array<{ tool_use_id: string; content: string }>;
          }
        | undefined;
      if (detail?.answers && detail.answers.length > 0) {
        void chatStream.send('', { approvedToolResult: detail.answers });
        return;
      }
      if (detail?.answer) {
        void chatStream.send(detail.answer);
      }
    };
    window.addEventListener('whisper-submit-answer', handler);
    return () => window.removeEventListener('whisper-submit-answer', handler);
  }, [chatStream]);

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
