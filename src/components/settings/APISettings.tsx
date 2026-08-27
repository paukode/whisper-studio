import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import { MODEL_OPTIONS, TRANSLATOR_OPTIONS, targetsFor } from '@/components/transcription/TranscriptionPanel';

interface ConfigData {
  tavily_api_key?: string;
  tavily_api_key_masked?: string;
  whisper_language?: string | null;
  bedrock_region?: string;
  transcription_backend?: string;
  translate_mode?: string;
  translate_target?: string;
}

/**
 * API / Keys settings tab.
 */
export const APISettings: React.FC = () => {
  const [tavilyKey, setTavilyKey] = useState('');
  const [tavilyHint, setTavilyHint] = useState('');
  const [whisperLang, setWhisperLang] = useState('');
  const [bedrockRegion, setBedrockRegion] = useState('');
  const [regionError, setRegionError] = useState('');
  const [modelValue, setModelValue] = useState('streaming');
  const [translateMode, setTranslateMode] = useState('off');
  const [translateTarget, setTranslateTarget] = useState('en');
  const [saveStatus, setSaveStatus] = useState('');

  // Load config on mount via TanStack Query
  const { data: configData } = useQuery({
    queryKey: ['config'],
    queryFn: () => get<ConfigData>('/api/config'),
  });

  // Seed the editable form fields when config data first arrives (or changes).
  // Done during render via the previous-value pattern rather than an effect
  // (React Compiler flags setState-in-effect). Only fires when the query data
  // identity changes, so it doesn't loop or clobber edits on every render.
  const [seededFrom, setSeededFrom] = useState<ConfigData | undefined>(undefined);
  if (configData && configData !== seededFrom) {
    setSeededFrom(configData);
    setWhisperLang(configData.whisper_language ?? '');
    setBedrockRegion(configData.bedrock_region || 'us-east-1');
    setModelValue(configData.transcription_backend ?? 'streaming');
    setTranslateMode(configData.translate_mode ?? 'off');
    setTranslateTarget(configData.translate_target ?? 'en');
    if (configData.tavily_api_key_masked) setTavilyHint(configData.tavily_api_key_masked);
  }

  const handleSave = useCallback(async () => {
    setSaveStatus('');
    // Block (don't clobber) on a blank region. An empty string would otherwise
    // be persisted and crash every Bedrock client with an invalid endpoint, so
    // tell the user to set it rather than silently overwriting a good value.
    const region = bedrockRegion.trim();
    if (!region) {
      setRegionError('Bedrock region is required (e.g. us-east-1).');
      document.getElementById('cfgBedrockRegion')?.focus();
      return;
    }
    setRegionError('');
    try {
      const option = MODEL_OPTIONS.find((o) => o.value === modelValue) ?? MODEL_OPTIONS[0];
      const body: Record<string, string> = {
        whisper_language: whisperLang,
        bedrock_region: region,
        transcription_backend: option.backend,
        translate_mode: translateMode,
        translate_target: translateTarget,
      };
      // Only send tavily key if user typed something new
      if (tavilyKey) {
        body.tavily_api_key = tavilyKey;
      }
      await put('/api/config', body);
      // Keep the shared store (and so the transcript header) in sync, and
      // apply to a live recording exactly like the header controls do.
      useSettingsStore.getState().updateConfig({
        transcriptionBackend: option.backend,
        translateMode,
        translateTarget,
      });
      window.dispatchEvent(
        new CustomEvent('whisper-set-model', { detail: { backend: option.backend } }),
      );
      window.dispatchEvent(
        new CustomEvent('whisper-set-translate', {
          detail: { mode: translateMode, target: translateTarget },
        }),
      );
      setSaveStatus('Saved!');

      // Update hint if a new key was provided
      if (tavilyKey.length > 8) {
        setTavilyHint(`${tavilyKey.slice(0, 4)}..${tavilyKey.slice(-4)}`);
      } else if (tavilyKey.length > 0) {
        setTavilyHint('••••');
      }
      setTavilyKey('');

      // Clear status after a few seconds
      setTimeout(() => setSaveStatus(''), 3000);
    } catch {
      setSaveStatus('Save failed');
    }
  }, [tavilyKey, whisperLang, bedrockRegion, modelValue, translateMode, translateTarget]);

  return (
    <div className="settings-form" style={{ maxWidth: 480 }}>
      <label htmlFor="cfgTavilyKey">Tavily API Key (for web search)</label>
      <input
        type="password"
        className="settings-input"
        id="cfgTavilyKey"
        placeholder="tvly-..."
        value={tavilyKey}
        onChange={(e) => setTavilyKey(e.target.value)}
      />
      <span className="settings-hint" id="cfgTavilyHint">{tavilyHint}</span>

      <label htmlFor="cfgWhisperLang">Whisper Language (blank = auto-detect)</label>
      <input
        type="text"
        className="settings-input"
        id="cfgWhisperLang"
        placeholder="e.g. pl — or pl,en for mixed-language sessions"
        value={whisperLang}
        onChange={(e) => setWhisperLang(e.target.value)}
      />
      <span className="settings-hint">
        One code pins the language. A comma-separated list (e.g. pl,en) keeps
        auto-detection but limits it to those languages, so short utterances in
        a mixed-language meeting are never misdetected or auto-translated.
      </span>

      <label htmlFor="cfgTranscriptionModel">Transcription Model</label>
      <select
        className="settings-input"
        id="cfgTranscriptionModel"
        value={modelValue}
        onChange={(e) => setModelValue(e.target.value)}
      >
        {MODEL_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>{o.label}</option>
        ))}
      </select>
      <span className="settings-hint">
        Same selector as the transcript header; applies live. Whisper Large
        v3: most accurate, 99 languages. Canary: fastest, best translation,
        25 European languages, detects the language per sentence (limited to
        the list above when one is set). Parakeet: words appear live as you
        speak, lowest latency.
      </span>

      <label htmlFor="cfgTranslateMode">Translation Model</label>
      <select
        className="settings-input"
        id="cfgTranslateMode"
        value={translateMode}
        onChange={(e) => setTranslateMode(e.target.value)}
      >
        {TRANSLATOR_OPTIONS.map((o) => (
          <option key={o.value} value={o.value}>
            {o.value === 'off' ? 'Off' : o.label}
          </option>
        ))}
      </select>
      <span className="settings-hint">
        Shows a translation line under speech in other languages, whichever
        model is transcribing. Canary translates between its 25 European
        languages and English (either direction, English always on one side)
        and detects the spoken language per sentence. Apple translates
        between any two of its ~20 languages, on-device and free, in the Mac
        app only.
      </span>

      {translateMode !== 'off' && (
        <>
          <label htmlFor="cfgTranslateTarget">Translate To</label>
          <select
            className="settings-input"
            id="cfgTranslateTarget"
            value={translateTarget}
            onChange={(e) => setTranslateTarget(e.target.value)}
          >
            {targetsFor(translateMode).map((t) => (
              <option key={t.code} value={t.code}>{t.name}</option>
            ))}
          </select>
          <span className="settings-hint">
            Language of the translation line. Canary reaches non-English
            targets from English speech only (English is always one side of
            its pair); Apple reaches any of its languages from anything.
          </span>
        </>
      )}

      <label htmlFor="cfgBedrockRegion">Bedrock Region</label>
      <input
        type="text"
        className="settings-input"
        id="cfgBedrockRegion"
        placeholder="us-east-1"
        value={bedrockRegion}
        aria-invalid={!!regionError}
        aria-describedby="cfgBedrockRegionError"
        onChange={(e) => {
          setBedrockRegion(e.target.value);
          if (regionError) setRegionError('');
        }}
      />
      {regionError && (
        <span
          id="cfgBedrockRegionError"
          className="settings-hint"
          role="alert"
          aria-live="assertive"
          style={{ color: 'var(--error, #f87171)' }}
        >
          {regionError}
        </span>
      )}

      <div style={{ marginTop: 12 }}>
        <button
          className="btn btn-primary btn-sm"
          id="cfgSaveBtn"
          type="button"
          onClick={handleSave}
        >
          Save
        </button>
        <span className="settings-hint" id="cfgSaveStatus" role="status" aria-live="polite" style={{ marginLeft: 8 }}>
          {saveStatus}
        </span>
      </div>
    </div>
  );
};
