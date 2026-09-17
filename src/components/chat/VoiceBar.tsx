import React from 'react';
import { useVoiceStore } from '@/stores/voiceStore';
import { voiceController } from '@/services/voiceController';

/**
 * The composer while a voice conversation is open. Same footprint as the text
 * box (74px, 10px radius) and the same 38px square buttons, so nothing else on
 * the screen moves: level orb + state + hint on the left, then Type instead,
 * Mute and End.
 */
const HINTS: Record<string, string> = {
  connecting: 'Connecting to the voice model…',
  listening: 'Speak naturally. It answers when you pause, and you can talk over it any time.',
  thinking: 'Working on it with the assistant. Keep talking if you like, it will catch up.',
  speaking: 'Talk over it to interrupt. Everything it says is written into the chat.',
  ending: 'Wrapping up…',
};

const LABELS: Record<string, string> = {
  connecting: 'Connecting',
  listening: 'Listening',
  thinking: 'Thinking',
  speaking: 'Speaking',
  ending: 'Ending',
};

export const VoiceBar: React.FC = () => {
  const status = useVoiceStore((s) => s.status);
  const muted = useVoiceStore((s) => s.muted);
  const voiceId = useVoiceStore((s) => s.voiceId);
  const voices = useVoiceStore((s) => s.voices);
  const error = useVoiceStore((s) => s.error);

  const voice = voices.find((v) => v.id === voiceId);
  const voiceLabel = voice ? `${voice.label} · ${voice.locale}` : voiceId;
  const hint = error ?? (muted && status === 'listening' ? 'Microphone muted. Unmute to keep talking.' : HINTS[status] ?? '');

  return (
    <div className="chat-input-row voice-row" data-testid="voice-bar">
      <div className={`voice-bar voice-${status}${muted ? ' muted' : ''}`} role="status" aria-live="polite">
        <span className="voice-orb" aria-hidden="true">
          <i /><i /><i /><i /><i />
        </span>
        <div className="voice-text">
          <div className="voice-state">{LABELS[status] ?? status}</div>
          <div className="voice-hint">{hint}</div>
        </div>
        <div className="voice-right" title="Voice and language">
          <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" fill="currentColor" stroke="none" />
            <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
          </svg>
          {voiceLabel}
        </div>
      </div>
      <button
        type="button"
        className="mic-btn voice-type-btn"
        title="Type instead (voice stays on)"
        aria-label="Type instead"
        onClick={() => useVoiceStore.getState().setTyping(true)}
      >
        <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
          <rect x="2" y="6" width="20" height="12" rx="2" /><line x1="6" y1="10" x2="6" y2="10" /><line x1="10" y1="10" x2="10" y2="10" /><line x1="14" y1="10" x2="14" y2="10" /><line x1="18" y1="10" x2="18" y2="10" /><line x1="8" y1="14" x2="16" y2="14" />
        </svg>
      </button>
      <button
        type="button"
        className={`mic-btn voice-mute-btn${muted ? ' active' : ''}`}
        title={muted ? 'Unmute microphone' : 'Mute microphone'}
        aria-label={muted ? 'Unmute microphone' : 'Mute microphone'}
        aria-pressed={muted}
        onClick={() => voiceController.setMuted(!muted)}
      >
        {muted ? (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <line x1="2" y1="2" x2="22" y2="22" /><path d="M18.89 13.23A7 7 0 0 0 19 11" /><path d="M5 11a7 7 0 0 0 11.2 5.6" /><path d="M15 9.34V5a3 3 0 0 0-5.68-1.33" /><path d="M9 9v2a3 3 0 0 0 5.12 2.12" /><line x1="12" y1="18" x2="12" y2="22" />
          </svg>
        ) : (
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
            <rect x="9" y="3" width="6" height="11" rx="3" /><path d="M19 11a7 7 0 0 1-14 0" /><line x1="12" y1="18" x2="12" y2="22" />
          </svg>
        )}
      </button>
      <button
        type="button"
        className="btn btn-chat-stop voice-end-btn"
        title="End voice conversation"
        aria-label="End voice conversation"
        onClick={() => voiceController.stop()}
      >
        <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
          <rect x="3" y="3" width="10" height="10" rx="2" fill="currentColor" />
        </svg>
      </button>
    </div>
  );
};
