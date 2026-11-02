/** The transcript export labels a translation line with its own target. */
import { afterEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen } from '@testing-library/react';

vi.mock('@/utils/downloadFile', () => ({ downloadFile: vi.fn() }));

import { TranscriptionPanel } from './TranscriptionPanel';
import { getActiveTranscriptionStore } from '@/stores/sessionRuntimes';
import { downloadFile } from '@/utils/downloadFile';
import type { TranscriptSegment } from '@/types/session';

afterEach(() => {
  getActiveTranscriptionStore().getState().loadSegments([], {});
  vi.clearAllMocks();
});

describe('transcript export', () => {
  it('writes [FR] for a French translation, not [EN]', () => {
    const seg = {
      id: 'seg-1',
      text: 'Good morning',
      speaker: 'Speaker 1',
      timestamp: Date.UTC(2026, 10, 2, 8, 0, 0),
      edited: false,
      translations: [{ chunkId: 1, text: 'Bonjour', target: 'fr' }],
    } as unknown as TranscriptSegment;
    getActiveTranscriptionStore().getState().loadSegments([seg], {});
    render(<TranscriptionPanel />);
    fireEvent.click(screen.getByText('Export'));
    const text = vi.mocked(downloadFile).mock.calls[0][0] as string;
    expect(text).toContain('    [FR] Bonjour');
    expect(text).not.toContain('[EN]');
  });
});
