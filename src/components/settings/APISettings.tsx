import React, { useCallback, useState } from 'react';
import { useQuery } from '@tanstack/react-query';
import { get, put } from '@/api/client';

interface ConfigData {
  tavily_api_key?: string;
  tavily_api_key_masked?: string;
  whisper_language?: string | null;
  bedrock_region?: string;
  transcription_backend?: string;
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
  const [transcriptionBackend, setTranscriptionBackend] = useState('streaming');
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
    setTranscriptionBackend(configData.transcription_backend ?? 'streaming');
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
      const body: Record<string, string> = {
        whisper_language: whisperLang,
        bedrock_region: region,
        transcription_backend: transcriptionBackend,
      };
      // Only send tavily key if user typed something new
      if (tavilyKey) {
        body.tavily_api_key = tavilyKey;
      }
      await put('/api/config', body);
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
  }, [tavilyKey, whisperLang, bedrockRegion, transcriptionBackend]);

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
        placeholder="e.g. en, es, pl"
        value={whisperLang}
        onChange={(e) => setWhisperLang(e.target.value)}
      />

      <label htmlFor="cfgTranscriptionBackend">Transcription Engine</label>
      <select
        className="settings-input"
        id="cfgTranscriptionBackend"
        value={transcriptionBackend}
        onChange={(e) => setTranscriptionBackend(e.target.value)}
      >
        <option value="whisper">Whisper (sentence-by-sentence)</option>
        <option value="streaming">Parakeet streaming (word-by-word)</option>
      </select>
      <span className="settings-hint">
        Applies to new recordings. Whisper finalizes whole utterances at pauses;
        Parakeet streams text live as you speak.
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
