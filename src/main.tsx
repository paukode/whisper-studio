import React from 'react';
import ReactDOM from 'react-dom/client';
import App from '@/App';
import { installCaptureHelperBridge } from '@/services/captureHelperBridge';
import { registerShellToastBridge } from '@/services/shellToastBridge';

// window.__whisperShellToast — lets the macOS shell surface download
// feedback ("Saved to Downloads: …") inside the app. No-op in browsers.
registerShellToastBridge();

// Source install in a browser: the helper's native mic and system/app audio
// capture behind the same bridge globals the Mac app window injects.
installCaptureHelperBridge().catch((err) => console.error('[capture-helper]', err));

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
