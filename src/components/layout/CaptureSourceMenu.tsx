import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useRecordingStore } from '@/stores/recordingStore';
import { useUIStore } from '@/stores/uiStore';

// In-app audio-source picker. The microphone is always captured; this
// lets the user ADD an optional Chrome/Edge tab-audio source, which the
// recording controller mixes into the same mono stream at start()
// (see recordingController.start()). It complements — does not replace —
// the older OS-level BlackHole aggregate-device path described at the top
// of Header.tsx: no system setup required, but Chrome/Edge only.

const TAB_AUDIO_SUPPORTED =
  typeof navigator !== 'undefined' &&
  !!navigator.mediaDevices &&
  typeof navigator.mediaDevices.getDisplayMedia === 'function';

// getDisplayMedia is blocked inside an embedded iframe (the dev preview),
// so we surface a clear reason there instead of a raw permission error.
const IN_IFRAME = typeof window !== 'undefined' && window.self !== window.top;

const HeadphonesIcon: React.FC<{ size?: number }> = ({ size = 15 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <path d="M3 18v-6a9 9 0 0 1 18 0v6" />
    <path d="M21 19a2 2 0 0 1-2 2h-1a1 1 0 0 1-1-1v-3a1 1 0 0 1 1-1h3zM3 19a2 2 0 0 0 2 2h1a1 1 0 0 0 1-1v-3a1 1 0 0 0-1-1H3z" />
  </svg>
);

export const CaptureSourceMenu: React.FC = () => {
  const tabStream = useRecordingStore((s) => s.tabStream);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const acquireTabAudio = useRecordingStore((s) => s.acquireTabAudio);
  const releaseTabAudio = useRecordingStore((s) => s.releaseTabAudio);
  const addToast = useUIStore((s) => s.addToast);

  const [open, setOpen] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  const tabActive = !!tabStream;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  const handleToggle = useCallback(() => setOpen((o) => !o), []);

  const handleChooseTab = useCallback(async () => {
    try {
      await acquireTabAudio();
    } catch (err) {
      const msg = err instanceof Error ? err.message : String(err);
      const name = err instanceof DOMException ? err.name : '';
      if (msg === 'no-audio') {
        addToast({
          type: 'error',
          message:
            'No tab audio shared. Turn on "Also share tab audio" in the dialog, then choose the tab again.',
          duration: 6000,
        });
      } else if (name === 'NotAllowedError') {
        // The user dismissed the picker — stay quiet, unless it's the
        // embedded-preview iframe blocking capture, which is worth saying.
        if (IN_IFRAME) {
          addToast({
            type: 'error',
            message:
              'Tab audio needs the app open in a real browser tab, not the embedded preview.',
            duration: 6000,
          });
        }
      } else {
        addToast({ type: 'error', message: `Couldn't capture tab audio: ${msg}`, duration: 6000 });
      }
    }
  }, [acquireTabAudio, addToast]);

  const handleRemoveTab = useCallback(() => releaseTabAudio(), [releaseTabAudio]);

  // Why the "Choose tab" action can't be used right now (null = usable).
  const tabDisabledReason = !TAB_AUDIO_SUPPORTED
    ? 'Only Chrome and Edge can share tab audio'
    : IN_IFRAME
      ? 'Open the app in a browser tab to capture tab audio'
      : isRecording
        ? 'Stop recording to add a source'
        : null;

  return (
    <div className="capture-source-wrap" ref={wrapRef}>
      <button
        className={`btn-icon capture-source-btn${tabActive ? ' has-tab' : ''}`}
        title="Audio sources"
        aria-label="Audio sources"
        aria-expanded={open}
        onClick={handleToggle}
        type="button"
      >
        <HeadphonesIcon size={16} />
      </button>

      <div className={`capture-source-menu${open ? ' open' : ''}`}>
        <div className="capture-source-title">Audio sources</div>

        <div className="capture-source-row">
          <span className="capture-source-icon mic">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M12 2a3 3 0 0 0-3 3v6a3 3 0 0 0 6 0V5a3 3 0 0 0-3-3z" />
              <path d="M19 10v1a7 7 0 0 1-14 0v-1" />
              <line x1="12" y1="18" x2="12" y2="22" />
              <line x1="8" y1="22" x2="16" y2="22" />
            </svg>
          </span>
          <div className="capture-source-text">
            <div className="capture-source-name">Microphone</div>
            <div className="capture-source-sub">Your headset or laptop mic</div>
          </div>
          <span className="capture-source-badge on">On</span>
        </div>

        <div className="capture-source-row">
          <span className="capture-source-icon tab">
            <svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <rect x="3" y="4" width="18" height="16" rx="2" />
              <line x1="3" y1="9" x2="21" y2="9" />
              <circle cx="6.5" cy="6.5" r="0.5" />
            </svg>
          </span>
          <div className="capture-source-text">
            <div className="capture-source-name">Chrome tab audio</div>
            <div className="capture-source-sub">
              {tabActive
                ? 'Capturing a browser tab'
                : (tabDisabledReason ?? 'Not added yet')}
            </div>
          </div>
          {tabActive ? (
            <button className="capture-source-action remove" onClick={handleRemoveTab} type="button">
              Stop
            </button>
          ) : (
            <button
              className="capture-source-action"
              onClick={handleChooseTab}
              type="button"
              disabled={!!tabDisabledReason}
              title={tabDisabledReason ?? undefined}
            >
              Choose tab
            </button>
          )}
        </div>

        <div className="capture-source-hint">
          <HeadphonesIcon size={14} />
          <span>Wear headphones so the tab's sound isn't picked up twice by your mic.</span>
        </div>
      </div>
    </div>
  );
};
