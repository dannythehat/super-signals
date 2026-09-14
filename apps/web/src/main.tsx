import React from 'react';
import ReactDOM from 'react-dom/client';

import { App } from './App';
import { installApiFetchGuard } from './apiFetchGuard';
import './styles.css';
import './navigation.css';
import './workspace-enhancements.css';
import './onboarding.css';
import './trading-controls.css';
import './dashboard-day32.css';
import './owner-manual-close.css';
import './settings-day32.css';
import './trade-timeline-day33.css';

// Install before React mounts so every JSON API read gets the same Render-transition
// protection. Mutating requests are never retried by the guard.
installApiFetchGuard();

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

function requestCanonicalAccountSync(): void {
  if (document.visibilityState !== 'visible') return;
  window.dispatchEvent(new Event('super-signals-ledger-synced'));
}

// Keep an already-open trading session on its current frontend build. New releases are
// picked up naturally on the next clean app launch/reload instead of forcing the app to
// reload while the user is viewing or managing trades.
window.addEventListener('focus', requestCanonicalAccountSync);
document.addEventListener('visibilitychange', requestCanonicalAccountSync);

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js', { updateViaCache: 'none' });
  });
}
