import React, { useCallback, useEffect, useRef, useState } from 'react';
import { useSessionStore } from '@/stores/sessionStore';
import { useUIStore } from '@/stores/uiStore';
import { useRecordingStore } from '@/stores/recordingStore';
import { useActiveTranscriptionStore } from '@/stores/sessionRuntimes';
import { useBackgroundTaskStore } from '@/stores/backgroundTaskStore';
import { recordingController } from '@/services/recordingController';
import { CaptureSourceMenu } from '@/components/layout/CaptureSourceMenu';
import { BackgroundTasksPanel } from '@/components/tasks/BackgroundTasksPanel';
import { useTheme } from '@/providers/ThemeProvider';
import type { ThemeKey } from '@/types/theme';

// The recording ENGINE (websocket, mic worklet, PCM buffering, watchdog,
// stop-drain protocol) lives in src/services/recordingController.ts as a
// module singleton bound to its OWNING session. This component only
// renders the buttons and the jump-back chip — which is what lets a
// recording keep running while the user switches sessions.
//
// Browser-level system-audio capture is intentionally NOT done here. The
// right cross-browser way to capture mic + system audio together is at the OS
// layer — a virtual loopback device (e.g. BlackHole 2ch on macOS) combined
// with the mic into one Aggregate Device, which getUserMedia() then picks up
// as a single input. See README → "Capturing system audio (macOS)".

const THEME_SWATCHES: Record<ThemeKey, string> = {
  auto: 'linear-gradient(135deg, #111113 50%, #f5f3ef 50%)',
  dark: '#111113',
  light: '#f5f3ef',
  'dark-high-contrast': '#000000',
  'light-high-contrast': '#ffffff',
  'dark-daltonized': 'linear-gradient(135deg, #111113 50%, #3b82f6 50%)',
  'light-daltonized': 'linear-gradient(135deg, #f5f3ef 50%, #2563eb 50%)',
  'dark-taw': 'linear-gradient(135deg, #262624 50%, #d97757 50%)',
  'light-taw': 'linear-gradient(135deg, #faf9f5 50%, #d97757 50%)',
};

export const Header: React.FC = () => {
  const createSession = useSessionStore((s) => s.createSession);
  const currentSessionId = useSessionStore((s) => s.currentSessionId);
  const sessions = useSessionStore((s) => s.sessions);
  const switchSession = useSessionStore((s) => s.switchSession);
  const toggleSidebar = useUIStore((s) => s.toggleSidebar);
  const openSettings = useUIStore((s) => s.openSettings);
  const sidebarCollapsed = useUIStore((s) => s.sidebarCollapsed);
  const transcriptVisible = useUIStore((s) => s.transcriptVisible);
  const toggleTranscript = useUIStore((s) => s.toggleTranscript);
  const isRecording = useRecordingStore((s) => s.isRecording);
  const recordingSessionId = useRecordingStore((s) => s.recordingSessionId);
  const tabAudioActive = useRecordingStore((s) => !!s.tabStream);
  const wsConnected = useRecordingStore((s) => s.isConnected);
  const segments = useActiveTranscriptionStore((s) => s.segments);
  const runningTaskCount = useBackgroundTaskStore((s) => s.runningCount);
  const taskPanelOpen = useBackgroundTaskStore((s) => s.panelOpen);
  const setTaskPanelOpen = useBackgroundTaskStore((s) => s.setPanelOpen);
  const { themeKey, setTheme, themes } = useTheme();

  const [pickerOpen, setPickerOpen] = useState(false);
  const pickerRef = useRef<HTMLDivElement>(null);

  // Recording-vs-viewed relationship drives the whole header-right UI.
  const recordingHere = isRecording && recordingSessionId === currentSessionId;
  const recordingElsewhere = isRecording && !recordingHere;
  const recordingTitle =
    sessions.find((s) => s.id === recordingSessionId)?.title ?? 'another session';

  const showToggleBtn = isRecording || segments.length > 0 || transcriptVisible;

  // Close theme picker on outside click
  useEffect(() => {
    if (!pickerOpen) return;
    const handleClickOutside = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) {
        setPickerOpen(false);
      }
    };
    document.addEventListener('mousedown', handleClickOutside);
    return () => document.removeEventListener('mousedown', handleClickOutside);
  }, [pickerOpen]);

  const handleTogglePicker = useCallback(() => setPickerOpen((p) => !p), []);
  const handleSelectTheme = useCallback((key: ThemeKey) => { setTheme(key); setPickerOpen(false); }, [setTheme]);
  const handleNewSession = useCallback(() => createSession(), [createSession]);
  const handleToggleSidebar = useCallback(() => toggleSidebar(), [toggleSidebar]);
  const handleOpenSettings = useCallback(() => openSettings(), [openSettings]);

  const handleStartRecording = useCallback(() => {
    let sid = useSessionStore.getState().currentSessionId;
    if (!sid) sid = useSessionStore.getState().createSession();
    void recordingController.start(sid);
  }, []);

  const handleStopRecording = useCallback(() => {
    recordingController.stop();
  }, []);

  const handleJumpToRecording = useCallback(() => {
    const sid = useRecordingStore.getState().recordingSessionId;
    if (sid) void switchSession(sid);
  }, [switchSession]);

  return (
    <header className="header">
      <div className="header-left">
        <button
          className="btn-icon sidebar-toggle-btn"
          id="sidebarToggle"
          title="Toggle sessions"
          onClick={handleToggleSidebar}
          type="button"
          aria-expanded={!sidebarCollapsed}
        >
          <svg width="16" height="16" viewBox="0 0 16 16" fill="none">
            <path d="M10 3L5 8L10 13" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"/>
          </svg>
        </button>
        <h1 className="app-title">
          <span className="app-title-mark">W</span>hisper Studio
        </h1>
        <button
          className="new-session-btn"
          id="newSessionBtn"
          title="New conversation"
          aria-label="New conversation"
          onClick={handleNewSession}
          type="button"
        >
          <svg width="14" height="14" viewBox="0 0 16 16" fill="none" stroke="currentColor" strokeWidth="2.5"><path d="M8 3v10M3 8h10"/></svg>
        </button>
      </div>

      <div className="header-center">
        {/* display:flex must be explicit — the base CSS class defaults to
          * display:none (it expects an .active modifier that React never
          * applied), so `undefined` here left the REC indicator invisible. */}
        <span
          className="recording-indicator"
          id="recordingIndicator"
          style={{ display: isRecording ? 'flex' : 'none' }}
        >
          <span className="pulse-dot"></span> REC
        </span>
        <span className="audio-source-label" id="audioSourceLabel">
          {isRecording ? (tabAudioActive ? 'Mic + Tab' : 'Mic') : ''}
        </span>
      </div>

      <div className="header-right">
        {/* Jump chip: a recording is live in another session — one click
          * returns to it. Recording survives the navigation either way. */}
        {recordingElsewhere && (
          <button
            className="btn recording-jump-chip"
            id="recordingJumpChip"
            type="button"
            title={`Recording in "${recordingTitle}" — click to jump back`}
            onClick={handleJumpToRecording}
          >
            <span className="pulse-dot"></span>
            <span className="recording-jump-title">{recordingTitle}</span>
          </button>
        )}

        {/* Audio-source picker: mic (always) + optional Chrome tab audio.
          * Sits beside Record so sources are armed just before starting. */}
        <CaptureSourceMenu />

        {!recordingHere ? (
          <button
            className="btn btn-record"
            id="recordBtn"
            type="button"
            disabled={recordingElsewhere}
            title={
              recordingElsewhere
                ? `Recording in "${recordingTitle}" — stop it before recording here`
                : undefined
            }
            onClick={handleStartRecording}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><circle cx="7" cy="7" r="5" fill="currentColor"/></svg>
            Record
          </button>
        ) : (
          <button
            className="btn btn-stop"
            id="stopBtn"
            type="button"
            onClick={handleStopRecording}
          >
            <svg width="14" height="14" viewBox="0 0 14 14" fill="none"><rect x="3" y="3" width="8" height="8" rx="1.5" fill="currentColor"/></svg>
            Stop
          </button>
        )}

        {/* Transcript toggle — visible when recording or segments exist */}
        <button
          className="btn-icon transcript-toggle-btn"
          id="transcriptToggleBtn"
          title={transcriptVisible ? 'Hide transcript' : 'Show transcript'}
          type="button"
          style={{ display: showToggleBtn ? undefined : 'none' }}
          onClick={toggleTranscript}
          aria-pressed={transcriptVisible}
        >
          {transcriptVisible ? (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M1 12s4-8 11-8 11 8 11 8-4 8-11 8-11-8-11-8z"/>
              <circle cx="12" cy="12" r="3"/>
            </svg>
          ) : (
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round">
              <path d="M17.94 17.94A10.07 10.07 0 0 1 12 20c-7 0-11-8-11-8a18.45 18.45 0 0 1 5.06-5.94"/>
              <path d="M9.9 4.24A9.12 9.12 0 0 1 12 4c7 0 11 8 11 8a18.5 18.5 0 0 1-2.16 3.19"/>
              <path d="M14.12 14.12a3 3 0 1 1-4.24-4.24"/>
              <line x1="1" y1="1" x2="23" y2="23"/>
            </svg>
          )}
        </button>

        {/* This reflects the transcription websocket, which is only live
          * while recording. On a fresh, idle load it's simply not connected —
          * that's normal, not an error. Show a calm "Ready" with a neutral dot
          * when idle, and the green "Connected" state only once the socket is
          * actually up, so the header never reads as broken. */}
        <span className="connection-status" id="connectionStatus">
          <span className={`status-dot${wsConnected ? ' connected' : ' ready'}`} id="statusDot"></span>
          <span id="statusText">{wsConnected ? 'Connected' : 'Ready'}</span>
        </span>

        {/* Background-task pill: only rendered while something is actually
          * running, so the header stays quiet in the common idle case. Click
          * opens the cross-session what's-running panel. */}
        {runningTaskCount > 0 && (
          <button
            className="btn bg-tasks-pill"
            id="bgTasksPill"
            type="button"
            title="Background tasks running — click to view"
            onClick={() => setTaskPanelOpen(!taskPanelOpen)}
            aria-expanded={taskPanelOpen}
          >
            <span className="pulse-dot"></span>
            {runningTaskCount} task{runningTaskCount === 1 ? '' : 's'}
          </button>
        )}
        <BackgroundTasksPanel />

        <button
          className="btn-icon"
          id="settingsBtn"
          title="Settings"
          onClick={handleOpenSettings}
          type="button"
        >
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
            <circle cx="12" cy="12" r="3"/>
            <path d="M19.4 15a1.65 1.65 0 0 0 .33 1.82l.06.06a2 2 0 0 1-2.83 2.83l-.06-.06a1.65 1.65 0 0 0-1.82-.33 1.65 1.65 0 0 0-1 1.51V21a2 2 0 0 1-4 0v-.09A1.65 1.65 0 0 0 9 19.4a1.65 1.65 0 0 0-1.82.33l-.06.06a2 2 0 0 1-2.83-2.83l.06-.06A1.65 1.65 0 0 0 4.68 15a1.65 1.65 0 0 0-1.51-1H3a2 2 0 0 1 0-4h.09A1.65 1.65 0 0 0 4.6 9a1.65 1.65 0 0 0-.33-1.82l-.06-.06a2 2 0 0 1 2.83-2.83l.06.06A1.65 1.65 0 0 0 9 4.68a1.65 1.65 0 0 0 1-1.51V3a2 2 0 0 1 4 0v.09a1.65 1.65 0 0 0 1 1.51 1.65 1.65 0 0 0 1.82-.33l.06-.06a2 2 0 0 1 2.83 2.83l-.06.06A1.65 1.65 0 0 0 19.4 9a1.65 1.65 0 0 0 1.51 1H21a2 2 0 0 1 0 4h-.09a1.65 1.65 0 0 0-1.51 1z"/>
          </svg>
        </button>

        <div className="theme-picker-wrap" ref={pickerRef}>
          <button
            className="btn-icon theme-toggle"
            id="themeToggle"
            title="Toggle theme"
            onClick={handleTogglePicker}
            type="button"
          >
            <svg className="icon-sun" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <circle cx="12" cy="12" r="5"/><line x1="12" y1="1" x2="12" y2="3"/><line x1="12" y1="21" x2="12" y2="23"/><line x1="4.22" y1="4.22" x2="5.64" y2="5.64"/><line x1="18.36" y1="18.36" x2="19.78" y2="19.78"/><line x1="1" y1="12" x2="3" y2="12"/><line x1="21" y1="12" x2="23" y2="12"/><line x1="4.22" y1="19.78" x2="5.64" y2="18.36"/><line x1="18.36" y1="5.64" x2="19.78" y2="4.22"/>
            </svg>
            <svg className="icon-moon" width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round">
              <path d="M21 12.79A9 9 0 1 1 11.21 3 7 7 0 0 0 21 12.79z"/>
            </svg>
          </button>

          <div className={`theme-picker${pickerOpen ? ' open' : ''}`}>
            {themes.map((t) => {
              const swatch = THEME_SWATCHES[t.key];
              const isGradient = swatch.includes('gradient');
              return (
                <button
                  key={t.key}
                  className={`theme-picker-item${themeKey === t.key ? ' active' : ''}`}
                  onClick={() => handleSelectTheme(t.key)}
                  type="button"
                >
                  <span
                    className="theme-picker-swatch"
                    style={{
                      background: isGradient ? swatch : swatch,
                      backgroundColor: isGradient ? undefined : swatch,
                    }}
                  />
                  {t.label}
                  <span className="theme-picker-check">✓</span>
                </button>
              );
            })}
          </div>
        </div>
      </div>
    </header>
  );
};
