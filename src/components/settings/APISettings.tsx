import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import { MODEL_OPTIONS, modelValueOf } from '@/components/transcription/TranscriptionPanel';

interface ConfigData {
  tavily_api_key?: string;
  tavily_api_key_masked?: string;
  whisper_language?: string | null;
  bedrock_region?: string;
  transcription_backend?: string;
  whisper_variant?: string;
  translate_mode?: string;
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
    setModelValue(
      modelValueOf(
        configData.transcription_backend ?? 'streaming',
        configData.whisper_variant ?? 'turbo',
      ),
    );
    setTranslateMode(configData.translate_mode ?? 'off');
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
        whisper_variant: option.variant,
        translate_mode: translateMode,
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
        whisperVariant: option.variant,
        translateMode,
      });
      window.dispatchEvent(
        new CustomEvent('whisper-set-model', {
          detail: { backend: option.backend, variant: option.variant },
        }),
      );
      window.dispatchEvent(
        new CustomEvent('whisper-set-translate', { detail: { mode: translateMode } }),
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
  }, [tavilyKey, whisperLang, bedrockRegion, modelValue, translateMode]);

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
        Same selector as the transcript header; applies live. Whisper Turbo:
        fast, 99 languages, detects the language per sentence — best for
        mixed-language meetings. Whisper Large v3: most accurate, sentences
        appear 1-2 s later. Canary: fastest and the best translation to
        English, but needs the session language set above (no auto-detect).
        Parakeet: words appear live as you speak, lowest latency.
      </span>

      <label htmlFor="cfgTranslateMode">Translate to English</label>
      <select
        className="settings-input"
        id="cfgTranslateMode"
        value={translateMode}
        onChange={(e) => setTranslateMode(e.target.value)}
      >
        <option value="off">Off</option>
        <option value="auto">Auto (best for the chosen model)</option>
        <option value="model">This model (re-decode the audio)</option>
        <option value="apple">Apple on-device (Mac app only)</option>
      </select>
      <span className="settings-hint">
        Shows an English line under non-English speech. Auto uses Canary's or
        Whisper Large v3's own translation, and Apple's on-device translator
        for the other models when running in the Mac app. Apple translates
        the finished sentence text, so it works with every model, including
        Parakeet; first use downloads the language pack once.
      </span>

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
