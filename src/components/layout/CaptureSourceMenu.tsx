import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useRecordingStore } from '@/stores/recordingStore';
import { useUIStore } from '@/stores/uiStore';
import {
  isNativeAudioAvailable,
  listNativeAudioSources,
  type NativeAudioSourceInfo,
} from '@/services/nativeAudioSource';

// In-app audio-source picker.
//
// Browser build: the microphone is always captured; the user can ADD an
// optional Chrome/Edge tab-audio source, which the recording controller
// mixes into the same mono stream at start() (see recordingController).
// It complements — does not replace — the older OS-level BlackHole
// aggregate-device path described at the top of Header.tsx.
//
// macOS shell build (window.__WHISPER_NATIVE_AUDIO.available): a "This Mac"
// section additionally offers native output capture via Core Audio process
// taps — "System audio" (everything) or one running app (e.g. Zoom). One
// native source at a time, combinable with the mic; turning the mic off
// while a native source is armed gives "native only" mode (no getUserMedia
// at all). The selection persists via recordingStore. When the bridge is
// absent this section disappears and the menu renders exactly as before.

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

const SpeakerIcon: React.FC<{ size?: number }> = ({ size = 15 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <polygon points="11 5 6 9 2 9 2 15 6 15 11 19 11 5" />
    <path d="M15.54 8.46a5 5 0 0 1 0 7.07" />
    <path d="M19.07 4.93a10 10 0 0 1 0 14.14" />
  </svg>
);

const AppWindowIcon: React.FC<{ size?: number }> = ({ size = 15 }) => (
  <svg width={size} height={size} viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
    <rect x="3" y="4" width="18" height="16" rx="2" />
    <line x1="10" y1="4" x2="10" y2="8" />
    <line x1="3" y1="8" x2="21" y2="8" />
  </svg>
);

export const CaptureSourceMenu: React.FC = () => {
  const tabStream = useRecordingStore((s) => s.tabStream);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const acquireTabAudio = useRecordingStore((s) => s.acquireTabAudio);
  const releaseTabAudio = useRecordingStore((s) => s.releaseTabAudio);
  const nativeSource = useRecordingStore((s) => s.nativeSource);
  const micEnabled = useRecordingStore((s) => s.micEnabled);
  const setNativeSource = useRecordingStore((s) => s.setNativeSource);
  const setMicEnabled = useRecordingStore((s) => s.setMicEnabled);
  const addToast = useUIStore((s) => s.addToast);

  const [open, setOpen] = useState(false);
  const [nativeApps, setNativeApps] = useState<NativeAudioSourceInfo[]>([]);
  const [nativeScanning, setNativeScanning] = useState(false);
  const wrapRef = useRef<HTMLDivElement>(null);

  const nativeAvailable = isNativeAudioAvailable();
  const tabActive = !!tabStream;
  const hasExtraSource = tabActive || !!nativeSource;

  useEffect(() => {
    if (!open) return;
    const onDown = (e: MouseEvent) => {
      if (wrapRef.current && !wrapRef.current.contains(e.target as Node)) setOpen(false);
    };
    document.addEventListener('mousedown', onDown);
    return () => document.removeEventListener('mousedown', onDown);
  }, [open]);

  // Refresh the per-app list every time the menu opens: pids and the set of
  // audio-producing apps change constantly, so a stale list is worse than a
  // brief "Scanning…" state. Sequence-guarded so a slow scan can't clobber
  // the results of a newer one.
  const scanSeq = useRef(0);
  const refreshNativeSources = useCallback(() => {
    if (!isNativeAudioAvailable()) return;
    scanSeq.current += 1;
    const seq = scanSeq.current;
    setNativeScanning(true);
    listNativeAudioSources()
      .then((sources) => {
        if (seq === scanSeq.current) setNativeApps(sources.filter((s) => s.pid >= 0));
      })
      .catch(() => {
        if (seq === scanSeq.current) setNativeApps([]);
      })
      .finally(() => {
        if (seq === scanSeq.current) setNativeScanning(false);
      });
  }, []);

  const handleToggle = useCallback(() => {
    if (!open) refreshNativeSources();
    setOpen(!open);
  }, [open, refreshNativeSources]);

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

  const handleUseSystemAudio = useCallback(() => {
    setNativeSource({ pid: -1, name: 'System audio' });
  }, [setNativeSource]);

  const handleUseApp = useCallback(
    (app: NativeAudioSourceInfo) => {
      setNativeSource({ pid: app.pid, name: app.name, bundleID: app.bundleID });
    },
    [setNativeSource],
  );

  const handleStopNative = useCallback(() => setNativeSource(null), [setNativeSource]);

  const handleToggleMic = useCallback(
    () => setMicEnabled(!useRecordingStore.getState().micEnabled),
    [setMicEnabled],
  );

  // Why the "Choose tab" action can't be used right now (null = usable).
  const tabDisabledReason = !TAB_AUDIO_SUPPORTED
    ? 'Only Chrome and Edge can share tab audio'
    : IN_IFRAME
      ? 'Open the app in a browser tab to capture tab audio'
      : isRecording
        ? 'Stop recording to add a source'
        : null;

  const nativeDisabledReason = isRecording ? 'Stop recording to change sources' : null;

  const systemActive = nativeSource?.pid === -1;
  const isActiveApp = (app: NativeAudioSourceInfo): boolean =>
    !!nativeSource &&
    nativeSource.pid !== -1 &&
    (nativeSource.bundleID ? nativeSource.bundleID === app.bundleID : nativeSource.pid === app.pid);

  // The armed app may not be in the freshly scanned list (quit, or silent
  // right now) — still show it so the user can see and clear the selection.
  const armedAppMissing =
    !!nativeSource && nativeSource.pid !== -1 && !nativeApps.some((a) => isActiveApp(a));

  return (
    <div className="capture-source-wrap" ref={wrapRef}>
      <button
        className={`btn-icon capture-source-btn${hasExtraSource ? ' has-tab' : ''}`}
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
            <div className="capture-source-sub">
              {micEnabled
                ? 'Your headset or laptop mic'
                : `Off — using ${nativeSource?.name ?? 'the native source'} only`}
            </div>
          </div>
          {nativeSource ? (
            <button
              className={`capture-source-action${micEnabled ? ' remove' : ''}`}
              onClick={handleToggleMic}
              type="button"
              disabled={!!nativeDisabledReason}
              title={nativeDisabledReason ?? undefined}
            >
              {micEnabled ? 'Turn off' : 'Turn on'}
            </button>
          ) : (
            <span className="capture-source-badge on">On</span>
          )}
        </div>

        {nativeAvailable && (
          <>
            <div className="capture-source-title">This Mac</div>

            <div className="capture-source-row">
              <span className="capture-source-icon native">
                <SpeakerIcon />
              </span>
              <div className="capture-source-text">
                <div className="capture-source-name">System audio</div>
                <div className="capture-source-sub">
                  {systemActive ? 'Capturing everything the Mac plays' : 'Everything the Mac plays'}
                </div>
              </div>
              {systemActive ? (
                <button
                  className="capture-source-action remove"
                  onClick={handleStopNative}
                  type="button"
                  disabled={!!nativeDisabledReason}
                  title={nativeDisabledReason ?? undefined}
                >
                  Stop
                </button>
              ) : (
                <button
                  className="capture-source-action"
                  onClick={handleUseSystemAudio}
                  type="button"
                  disabled={!!nativeDisabledReason}
                  title={nativeDisabledReason ?? undefined}
                >
                  Use
                </button>
              )}
            </div>

            <div className="capture-source-apps">
              {armedAppMissing && nativeSource && (
                <div className="capture-source-row">
                  <span className="capture-source-icon native">
                    <AppWindowIcon />
                  </span>
                  <div className="capture-source-text">
                    <div className="capture-source-name">{nativeSource.name}</div>
                    <div className="capture-source-sub">Not playing audio right now</div>
                  </div>
                  <button
                    className="capture-source-action remove"
                    onClick={handleStopNative}
                    type="button"
                    disabled={!!nativeDisabledReason}
                    title={nativeDisabledReason ?? undefined}
                  >
                    Stop
                  </button>
                </div>
              )}
              {nativeApps.map((app) => (
                <div className="capture-source-row" key={app.bundleID ?? `pid-${app.pid}`}>
                  <span className="capture-source-icon native">
                    <AppWindowIcon />
                  </span>
                  <div className="capture-source-text">
                    <div className="capture-source-name">{app.name}</div>
                    <div className="capture-source-sub">
                      {isActiveApp(app) ? 'Capturing this app' : 'App audio output'}
                    </div>
                  </div>
                  {isActiveApp(app) ? (
                    <button
                      className="capture-source-action remove"
                      onClick={handleStopNative}
                      type="button"
                      disabled={!!nativeDisabledReason}
                      title={nativeDisabledReason ?? undefined}
                    >
                      Stop
                    </button>
                  ) : (
                    <button
                      className="capture-source-action"
                      onClick={() => handleUseApp(app)}
                      type="button"
                      disabled={!!nativeDisabledReason}
                      title={nativeDisabledReason ?? undefined}
                    >
                      Use
                    </button>
                  )}
                </div>
              ))}
              {nativeScanning && nativeApps.length === 0 && (
                <div className="capture-source-empty">Scanning for apps playing audio…</div>
              )}
              {!nativeScanning && nativeApps.length === 0 && !armedAppMissing && (
                <div className="capture-source-empty">
                  No apps are playing audio right now. Start playback (e.g. join the call), then
                  reopen this menu.
                </div>
              )}
            </div>
          </>
        )}

        {!nativeAvailable && (
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
        )}

        <div className="capture-source-hint">
          <HeadphonesIcon size={14} />
          <span>
            Wear headphones so captured audio isn&apos;t picked up twice by your mic.
          </span>
        </div>
      </div>
    </div>
  );
};
