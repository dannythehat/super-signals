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

const BUILD_CHECK_INTERVAL_MS = 30_000;
let reloadStarted = false;

function currentModuleScript(): string | null {
  const script = document.querySelector<HTMLScriptElement>('script[type="module"][src]');
  return script?.src || null;
}

async function reloadForNewBuild(): Promise<void> {
  if (reloadStarted) return;
  const current = currentModuleScript();
  if (!current) return;

  try {
    const response = await fetch(`/?__super_signals_build=${Date.now()}`, {
      cache: 'no-store',
      headers: { Accept: 'text/html' },
    });
    if (!response.ok) return;
    const html = await response.text();
    const parsed = new DOMParser().parseFromString(html, 'text/html');
    const latestPath = parsed.querySelector<HTMLScriptElement>('script[type="module"][src]')?.getAttribute('src');
    if (!latestPath) return;
    const latest = new URL(latestPath, window.location.origin).href;
    if (latest !== current) {
      reloadStarted = true;
      window.location.reload();
    }
  } catch {
    // A deployment or brief network interruption is not an authentication failure.
    // Keep the current app running and try the build check again later.
  }
}

function requestCanonicalAccountSync(): void {
  if (document.visibilityState !== 'visible') return;
  window.dispatchEvent(new Event('super-signals-ledger-synced'));
}

void reloadForNewBuild();
window.setInterval(() => void reloadForNewBuild(), BUILD_CHECK_INTERVAL_MS);
window.addEventListener('focus', () => void reloadForNewBuild());

// MobileDashboard already owns its bounded 15-second refresh cadence. Do not add a
// second global timer here: overlapping broker-backed reads can queue behind MetaAPI
// retries and make the single web worker appear unavailable to Render. Focus and
// visibility changes still request an immediate refresh for a returning user.
window.addEventListener('focus', requestCanonicalAccountSync);
document.addEventListener('visibilitychange', requestCanonicalAccountSync);

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js').then((registration) => {
      void registration.update();
      window.setInterval(() => void registration.update(), BUILD_CHECK_INTERVAL_MS);
    });
  });

  navigator.serviceWorker.addEventListener('controllerchange', () => {
    if (reloadStarted) return;
    reloadStarted = true;
    window.location.reload();
  });
}
