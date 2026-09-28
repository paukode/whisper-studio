/**
 * The Transcription Languages field must say what a blank value actually
 * means (server/asr/languages.py): for Canary, English plus the Mac's
 * languages, which on an English-only Mac pins English. The effective set
 * comes from GET /api/config's read-only _transcription_languages, and the
 * field still saves whisper_language exactly as typed.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest';
import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  put: vi.fn(),
}));
vi.mock('@/api/client', () => api);

import { APISettings } from '@/components/settings/APISettings';

type Info = {
  auto: string[];
  mac: string[];
  mac_error: string | null;
  canary: string[];
  canary_source: 'setting' | 'auto';
  whisper: string[];
  dropped: { canary: string[]; whisper: string[] };
};

function info(overrides: Partial<Info>): Info {
  return {
    auto: ['en', 'pl'],
    mac: ['en', 'pl'],
    mac_error: null,
    canary: ['en', 'pl'],
    canary_source: 'auto',
    whisper: [],
    dropped: { canary: [], whisper: [] },
    ...overrides,
  };
}

function renderWith(whisperLanguage: string, languages: Info) {
  api.get.mockResolvedValue({
    whisper_language: whisperLanguage,
    bedrock_region: 'us-east-1',
    _transcription_languages: languages,
  });
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <APISettings />
    </QueryClientProvider>,
  );
}

const field = () => screen.getByLabelText('Transcription Languages');
const inUse = async () => (await screen.findByText(/^In use:/)).textContent ?? '';

beforeEach(() => {
  vi.clearAllMocks();
  api.put.mockResolvedValue({ updated: true });
});

describe('APISettings transcription languages', () => {
  it('shows the automatic set a blank value resolves to', async () => {
    renderWith('', info({}));
    await waitFor(() => expect(field()).toHaveAttribute('placeholder', 'Auto: English, Polish'));
    const text = await inUse();
    expect(text).toContain('Canary uses English, Polish');
    expect(text).toContain('Whisper detects the language itself');
  });

  it('says plainly when the automatic set is English only', async () => {
    renderWith('', info({ auto: ['en'], mac: ['en'], canary: ['en'] }));
    await waitFor(() => expect(field()).toHaveAttribute('placeholder', 'Auto: English'));
    const text = await inUse();
    // It recommends the pair, since a single code would pin that language.
    expect(text).toContain('Auto: English only. To transcribe Polish as well, enter en,pl');
    expect(text).not.toContain('Add a code such as pl');
  });

  it('reports codes an engine cannot transcribe instead of hiding them', async () => {
    renderWith(
      'pl,ja',
      info({
        canary: ['pl'],
        canary_source: 'setting',
        whisper: ['pl', 'ja'],
        dropped: { canary: ['ja'], whisper: [] },
      }),
    );
    const text = await inUse();
    expect(text).toContain('Canary uses Polish');
    expect(text).toContain('cannot transcribe ja');
    expect(text).toContain('Whisper uses Polish, Japanese');
  });

  it('says Canary refuses a value it cannot use at all, with no substitute', async () => {
    renderWith(
      'ja',
      info({
        canary: [],
        canary_source: 'setting',
        whisper: ['ja'],
        dropped: { canary: ['ja'], whisper: [] },
      }),
    );
    const text = await inUse();
    expect(text).toContain('Canary cannot transcribe ja, so it will not transcribe');
    expect(text).not.toContain('Canary uses');
    expect(text).toContain('Whisper uses Japanese');
  });

  it('saves the typed value verbatim under the historical key', async () => {
    renderWith('', info({}));
    await waitFor(() => expect(field()).toHaveAttribute('placeholder', 'Auto: English, Polish'));
    fireEvent.change(field(), { target: { value: 'pl,en' } });
    fireEvent.click(screen.getByRole('button', { name: 'Save' }));
    await waitFor(() => expect(api.put).toHaveBeenCalled());
    expect(api.put.mock.calls[0][1]).toMatchObject({ whisper_language: 'pl,en' });
    expect(api.put.mock.calls[0][1]).not.toHaveProperty('_transcription_languages');
  });
});
