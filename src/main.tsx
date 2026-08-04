import React from 'react';
import ReactDOM from 'react-dom/client';
import App from '@/App';
import { registerShellToastBridge } from '@/services/shellToastBridge';

// window.__whisperShellToast — lets the macOS shell surface download
// feedback ("Saved to Downloads: …") inside the app. No-op in browsers.
registerShellToastBridge();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
