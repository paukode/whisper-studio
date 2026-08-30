import { useCallback, useState } from 'react';
import { useUIStore } from '@/stores/uiStore';

/**
 * One-time first-start notice: fresh installs now default to Local mode, and
 * this explains what the three model modes are and where to change them
 * (Settings > Model mode). Shown once per browser profile (the Mac app is a
 * single webview, so effectively once per install); the flag is written on
 * either button so it never comes back. Deliberately no ESC/backdrop dismiss:
 * two explicit buttons, and no interference with the global ESC kill switch.
 */
const SEEN_KEY = 'whisper_mode_notice_v1';

function seen(): boolean {
  try {
    return localStorage.getItem(SEEN_KEY) === '1';
  } catch {
    return true; // storage unavailable — never nag on every load
  }
}

function markSeen(): void {
  try {
    localStorage.setItem(SEEN_KEY, '1');
  } catch {
    /* storage unavailable */
  }
}

const MODES: { name: string; desc: string }[] = [
  {
    name: 'Local',
    desc:
      'Everything runs on this Mac and nothing leaves it. Transcription works ' +
      'out of the box; for chat, install an on-device model from Settings > ' +
      'Models > Discover. You start in this mode.',
  },
  {
    name: 'Hybrid',
    desc:
      'On-device where it is cheap and private (embeddings, entity extraction), ' +
      'cloud models where they are strongest (chat, summaries). Needs AWS ' +
      'credentials for the cloud side.',
  },
  {
    name: 'Cloud',
    desc: 'Every capability runs on cloud models via your AWS account.',
  },
];

export const ModelModeNotice: React.FC = () => {
  const [open, setOpen] = useState(() => !seen());
  const openSettings = useUIStore((s) => s.openSettings);

  const dismiss = useCallback(() => {
    markSeen();
    setOpen(false);
  }, []);

  const goToSettings = useCallback(() => {
    markSeen();
    setOpen(false);
    openSettings('model-mode');
  }, [openSettings]);

  if (!open) return null;

  return (
    <div className="whisper-dialog-overlay">
      <div
        className="whisper-dialog whisper-dialog--md"
        role="dialog"
        aria-modal="true"
        aria-labelledby="mode-notice-title"
        onClick={(e) => e.stopPropagation()}
      >
        <div className="whisper-dialog__header">
          <h2 className="whisper-dialog__title" id="mode-notice-title">
            You&apos;re starting in Local mode
          </h2>
        </div>
        <div className="whisper-dialog__body">
          <p style={{ marginTop: 0 }}>
            Whisper Studio can run its models in three ways. You can switch at any
            time in Settings &gt; Model mode.
          </p>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {MODES.map((m) => (
              <div
                key={m.name}
                style={{
                  padding: '10px 14px',
                  borderRadius: 10,
                  background: 'var(--bg-elevated)',
                  border: '1px solid var(--border)',
                }}
              >
                <div style={{ fontWeight: 600 }}>{m.name}</div>
                <div style={{ fontSize: 'var(--fs-caption)', color: 'var(--text-muted)' }}>
                  {m.desc}
                </div>
              </div>
            ))}
          </div>
        </div>
        <div className="whisper-dialog__footer">
          <button className="btn" onClick={goToSettings} type="button">
            Open model settings
          </button>
          <button className="btn btn-primary" onClick={dismiss} type="button">
            Got it
          </button>
        </div>
      </div>
    </div>
  );
};
