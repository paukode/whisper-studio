import React, { useCallback, useState } from 'react';
import { useQuery, useQueryClient } from '@tanstack/react-query';
import { get, put } from '@/api/client';
import { useSettingsStore } from '@/stores/settingsStore';
import {
  MODEL_OPTIONS,
  TRANSLATOR_UNAVAILABLE_NOTICE,
  isTranslatorUnavailable,
  targetsFor,
  translatorOptions,
} from '@/components/transcription/TranscriptionPanel';
import { isNativeTranslationAvailable } from '@/services/nativeTranslation';
import { HelpTip } from '@/components/common/HelpTip';

interface ConfigData {
  tavily_api_key?: string;
  tavily_api_key_masked?: string;
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
  const [bedrockRegion, setBedrockRegion] = useState('');
  const [regionError, setRegionError] = useState('');
  const [modelValue, setModelValue] = useState('streaming');
  const [translateMode, setTranslateMode] = useState('off');
  // Probed once: the shell injects its bridge at documentStart, so this can
  // never flip while the settings tab is open.
  const [appleAvailable] = useState(() => isNativeTranslationAvailable());
  const [translateTarget, setTranslateTarget] = useState('en');
  const [saveStatus, setSaveStatus] = useState('');
  const queryClient = useQueryClient();

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
      // Refetch so the form reflects what was just saved.
      void queryClient.invalidateQueries({ queryKey: ['config'] });
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
  }, [tavilyKey, bedrockRegion, modelValue, translateMode, translateTarget, queryClient]);

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

      <div className="settings-label-row">
        <label htmlFor="cfgTranscriptionModel">Transcription Model</label>
        <HelpTip text="Same selector as the transcript header; applies live. Whisper Large v3: most accurate, 99 languages. Parakeet: words appear live as you speak, lowest latency." />
      </div>
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
      <span className="settings-hint">Parakeet is live, Whisper is most accurate.</span>

      <div className="settings-label-row">
        <label htmlFor="cfgTranslateMode">Translation Model</label>
        <HelpTip text="Shows a translation line under speech in other languages, whichever model is transcribing. Canary translates between its 25 European languages and English (either direction, English always on one side) and detects the spoken language per sentence. Apple translates between any two of its ~20 languages, on-device and free, in the Mac app only." />
      </div>
      <select
        className="settings-input"
        id="cfgTranslateMode"
        aria-describedby={
          isTranslatorUnavailable(translateMode, appleAvailable)
            ? 'cfgTranslateModeWarning'
            : undefined
        }
        value={translateMode}
        onChange={(e) => setTranslateMode(e.target.value)}
      >
        {translatorOptions(appleAvailable).map((o) => (
          <option key={o.value} value={o.value} disabled={o.disabled}>
            {o.value === 'off' ? 'Off' : o.label}
          </option>
        ))}
      </select>
      {isTranslatorUnavailable(translateMode, appleAvailable) && (
        <span
          className="settings-hint settings-hint--warn"
          id="cfgTranslateModeWarning"
          role="status"
        >
          {TRANSLATOR_UNAVAILABLE_NOTICE}
        </span>
      )}
      <span className="settings-hint">Adds a translated line under speech in other languages.</span>

      {translateMode !== 'off' && (
        <>
          <div className="settings-label-row">
            <label htmlFor="cfgTranslateTarget">Translate To</label>
            <HelpTip text="Language of the translation line. Canary reaches non-English targets from English speech only (English is always one side of its pair); Apple reaches any of its languages from anything." />
          </div>
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
          style={{ color: 'var(--error)' }}
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
