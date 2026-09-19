/**
 * The translator picker must never offer Apple as if it worked everywhere:
 * outside the Mac app the server resolves an Apple selection to no translator
 * at all (server/websocket.py::resolve_translator) and no translation line is
 * produced. Both pickers (transcript header and Settings) derive their options
 * from the same helper, so these cover the shared derivation plus the rendered
 * result in Settings, available and unavailable.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { render, screen, waitFor } from '@testing-library/react';
import { QueryClient, QueryClientProvider } from '@tanstack/react-query';

const api = vi.hoisted(() => ({
  get: vi.fn(),
  post: vi.fn(),
  del: vi.fn(),
  put: vi.fn(),
}));
vi.mock('@/api/client', () => api);

import { APISettings } from '@/components/settings/APISettings';
import {
  APPLE_TRANSLATE_REQUIREMENT,
  TRANSLATOR_UNAVAILABLE_BADGE,
  TRANSLATOR_UNAVAILABLE_NOTICE,
  TranscriptionPanel,
  isTranslatorUnavailable,
  translatorOptions,
} from '@/components/transcription/TranscriptionPanel';
import { useSettingsStore } from '@/stores/settingsStore';

/** Install (or remove) the shell's native-translation bridge. The real
 *  detector wants both the documentStart marker and the WKWebView message
 *  handler, so fake both rather than stubbing the service. */
function setNativeBridge(available: boolean): void {
  const w = window as unknown as Record<string, unknown>;
  if (!available) {
    delete w.__WHISPER_NATIVE_TRANSLATE;
    delete w.webkit;
    return;
  }
  w.__WHISPER_NATIVE_TRANSLATE = { available: true };
  w.webkit = { messageHandlers: { nativeTranslate: { postMessage: () => {} } } };
}

const appleOption = (appleAvailable: boolean) =>
  translatorOptions(appleAvailable).find((o) => o.value === 'apple');

function renderSettings() {
  const qc = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <QueryClientProvider client={qc}>
      <APISettings />
    </QueryClientProvider>,
  );
}

/** The Apple <option> inside the Settings "Translation Model" picker. */
const appleSettingsOption = () =>
  screen.getByLabelText('Translation Model').querySelector<HTMLOptionElement>('option[value="apple"]');

/** The transcript header's picker (its aria-label, not the Settings label). */
const headerPicker = () => screen.getByLabelText('Translation model');
const appleHeaderOption = () =>
  headerPicker().querySelector<HTMLOptionElement>('option[value="apple"]');

beforeEach(() => {
  vi.clearAllMocks();
  api.get.mockResolvedValue({ translate_mode: 'apple', translate_target: 'en' });
  useSettingsStore.setState((s) => ({ config: { ...s.config, translateMode: 'apple' } }));
});

afterEach(() => {
  setNativeBridge(false);
});

describe('translatorOptions', () => {
  it('offers every translator when the native bridge is there', () => {
    const options = translatorOptions(true);
    expect(options.map((o) => o.value)).toEqual(['off', 'canary', 'apple']);
    expect(appleOption(true)).toEqual({ value: 'apple', label: 'Apple (any pair)' });
    expect(options.some((o) => o.disabled)).toBe(false);
  });

  it('keeps Apple listed but disabled, with the reason, without the bridge', () => {
    const apple = appleOption(false);
    // Still listed: a config already saved as apple must have an option to
    // render as its selected value.
    expect(apple?.disabled).toBe(true);
    expect(apple?.label).toContain(APPLE_TRANSLATE_REQUIREMENT);
    // Canary is unaffected either way: it runs server-side for every client.
    expect(translatorOptions(false).find((o) => o.value === 'canary')?.disabled).toBeUndefined();
  });

  it('flags only an Apple selection made where Apple cannot run', () => {
    expect(isTranslatorUnavailable('apple', false)).toBe(true);
    expect(isTranslatorUnavailable('apple', true)).toBe(false);
    expect(isTranslatorUnavailable('canary', false)).toBe(false);
    expect(isTranslatorUnavailable('off', false)).toBe(false);
  });
});

describe('APISettings translation picker', () => {
  it('leaves Apple selectable and says nothing extra inside the Mac app', async () => {
    setNativeBridge(true);
    renderSettings();
    await waitFor(() => expect(appleSettingsOption()).not.toBeNull());
    expect(appleSettingsOption()?.disabled).toBe(false);
    expect(appleSettingsOption()?.textContent).toBe('Apple (any pair)');
    expect(screen.queryByText(TRANSLATOR_UNAVAILABLE_NOTICE)).not.toBeInTheDocument();
  });

  it('explains a saved Apple selection that cannot run in a browser', async () => {
    renderSettings();
    // The saved value still renders as selected, so the picker never appears
    // to have silently switched to another translator.
    await waitFor(() => expect(screen.getByLabelText('Translation Model')).toHaveValue('apple'));
    expect(appleSettingsOption()?.disabled).toBe(true);
    expect(appleSettingsOption()?.textContent).toContain(APPLE_TRANSLATE_REQUIREMENT);
    expect(await screen.findByText(TRANSLATOR_UNAVAILABLE_NOTICE)).toBeInTheDocument();
  });

  it('drops the warning once a translator that runs here is picked', async () => {
    api.get.mockResolvedValue({ translate_mode: 'canary', translate_target: 'en' });
    renderSettings();
    await waitFor(() => expect(screen.getByLabelText('Translation Model')).toHaveValue('canary'));
    expect(screen.queryByText(TRANSLATOR_UNAVAILABLE_NOTICE)).not.toBeInTheDocument();
  });
});

describe('TranscriptionPanel translation picker', () => {
  it('marks the saved Apple selection unavailable in the transcript header', () => {
    render(<TranscriptionPanel />);
    expect(headerPicker()).toHaveValue('apple');
    expect(appleHeaderOption()?.disabled).toBe(true);
    expect(appleHeaderOption()?.textContent).toContain(APPLE_TRANSLATE_REQUIREMENT);
    // The badge carries the short form; the full notice is its tooltip.
    const badge = screen.getByText(TRANSLATOR_UNAVAILABLE_BADGE);
    expect(badge).toHaveAttribute('title', TRANSLATOR_UNAVAILABLE_NOTICE);
    // ...and the picker drops the accent-lit "translation is on" look, which
    // would otherwise claim a translation line that never arrives.
    expect(headerPicker().className).not.toContain(' on');
  });

  it('offers Apple normally, with no badge, inside the Mac app', () => {
    setNativeBridge(true);
    render(<TranscriptionPanel />);
    expect(appleHeaderOption()?.disabled).toBe(false);
    expect(screen.queryByText(TRANSLATOR_UNAVAILABLE_BADGE)).not.toBeInTheDocument();
    expect(headerPicker().className).toContain(' on');
  });
});
