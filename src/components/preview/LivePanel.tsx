import React, { useEffect, useRef, useState } from 'react';
import { createScreencastSocket } from '@/api/preview';

/**
 * LivePanel — the body of a live-preview dock panel. Streams the assistant's
 * headless preview browser for session `name` via the CDP screencast
 * WebSocket and renders the JPEG frames. View-only; the dock owns open/close.
 *
 * (Extracted from the former standalone LivePreviewPane so the same screencast
 * body can live inside the generalized RightDock.)
 */
const RECONNECT_MS = 1500;
type Status = 'connecting' | 'live' | 'closed';

export const LivePanel: React.FC<{ name: string }> = ({ name }) => {
  const [frame, setFrame] = useState<string | null>(null);
  const [status, setStatus] = useState<Status>('connecting');
  const [nonce, setNonce] = useState(0);
  const closingRef = useRef(false);

  useEffect(() => {
    closingRef.current = false;
    const ws = createScreencastSocket(name);
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;

    ws.onmessage = (ev) => {
      setFrame(ev.data as string);
      setStatus('live');
    };
    ws.onclose = () => {
      setStatus('closed');
      if (!closingRef.current) {
        reconnectTimer = setTimeout(() => setNonce((n) => n + 1), RECONNECT_MS);
      }
    };
    ws.onerror = () => {
      try { ws.close(); } catch { /* noop */ }
    };

    return () => {
      closingRef.current = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      try { ws.close(); } catch { /* noop */ }
    };
  }, [name, nonce]);

  return (
    <div
      style={{
        flex: '1 1 auto', minHeight: 0, overflow: 'auto',
        display: 'flex', alignItems: 'flex-start', justifyContent: 'center',
        background: 'var(--bg-inset, #0d0d0d)', padding: 8,
      }}
    >
      {frame ? (
        <img
          src={`data:image/jpeg;base64,${frame}`}
          alt="Live preview"
          style={{ width: '100%', height: 'auto', display: 'block', borderRadius: 4 }}
        />
      ) : (
        <div style={{ margin: 'auto', color: 'var(--text-muted, #888)', fontSize: 13, textAlign: 'center' }}>
          {status === 'closed'
            ? 'Reconnecting to the preview browser…'
            : 'Waiting for the preview browser…'}
        </div>
      )}
    </div>
  );
};

export default LivePanel;
