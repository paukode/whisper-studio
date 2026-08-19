/**
 * DOM-level wiring test for paste-to-attach: a paste event carrying files on
 * the composer textarea must become an attachment chip (uploading badge, then
 * the real id from /api/upload), while a plain text paste leaves the chips
 * alone. The hook unit tests cover the handler logic; this pins the textarea
 * onPaste wiring itself.
 */
import { render, act, fireEvent, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';
import type { ReactNode } from 'react';
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest';

vi.mock('@/hooks/useChatInputMic', () => ({
  useChatInputMic: () => ({
    isRecording: false, isConnecting: false, error: null,
    start: vi.fn(), stop: vi.fn(), toggle: vi.fn(),
  }),
}));
vi.mock('@/hooks/useChatStream', () => ({
  useChatStream: () => ({ send: vi.fn(() => Promise.resolve()), abort: vi.fn() }),
}));

import { ChatInput } from './ChatInput';
import { ThemeProvider } from '@/providers/ThemeProvider';

const renderChatInput = () => {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  const wrapper = ({ children }: { children: ReactNode }) => (
    <QueryClientProvider client={client}>
      <ThemeProvider>{children}</ThemeProvider>
    </QueryClientProvider>
  );
  return render(<ChatInput sessionId="s1" />, { wrapper });
};

describe('ChatInput paste-to-attach', () => {
  const realFetch = globalThis.fetch;
  let resolveUpload: (chips: { id: string; filename: string }[]) => void;

  beforeEach(() => {
    globalThis.fetch = vi.fn((input: RequestInfo | URL) => {
      if (String(input).includes('/api/upload')) {
        return new Promise((resolve) => {
          resolveUpload = (chips) =>
            resolve(new Response(JSON.stringify({ attachments: chips }), { status: 200 }));
        });
      }
      // Everything else ChatInput polls on mount is irrelevant here.
      return Promise.resolve(new Response('{}', { status: 200 }));
    }) as typeof fetch;
  });
  afterEach(() => { globalThis.fetch = realFetch; });

  it('turns a pasted file into an attachment chip', async () => {
    const { container } = renderChatInput();
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;
    const file = new File(['x'], 'shot.png', { type: 'image/png' });

    await act(async () => {
      fireEvent.paste(textarea, { clipboardData: { files: [file] } });
    });

    // Instant placeholder chip while the upload is in flight.
    const chip = container.querySelector('.attachment-chip') as HTMLElement;
    expect(chip).not.toBeNull();
    expect(chip.textContent).toContain('shot.png');
    expect(chip.querySelector('.attachment-uploading')).not.toBeNull();

    // Upload resolves → the chip swaps to the real attachment id.
    await act(async () => { resolveUpload([{ id: 'att-1', filename: 'shot.png' }]); });
    await waitFor(() => {
      expect(container.querySelector('.attachment-uploading')).toBeNull();
    });
    expect(container.querySelector('.attachment-chip')?.textContent).toContain('shot.png');
  });

  it('leaves a text-only paste to the textarea', async () => {
    const { container } = renderChatInput();
    const textarea = container.querySelector('textarea') as HTMLTextAreaElement;

    await act(async () => {
      fireEvent.paste(textarea, { clipboardData: { files: [], getData: () => 'plain text' } });
    });

    expect(container.querySelector('.attachment-chip')).toBeNull();
  });
});
