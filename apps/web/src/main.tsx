import React from 'react';
import ReactDOM from 'react-dom/client';

import { App } from './App';
import './styles.css';
import './navigation.css';
import './workspace-enhancements.css';
import './onboarding.css';
import './trading-controls.css';
import './dashboard-day32.css';
import './owner-manual-close.css';
import './settings-day32.css';
import './trade-timeline-day33.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);

if ('serviceWorker' in navigator) {
  window.addEventListener('load', () => {
    void navigator.serviceWorker.register('/sw.js');
  });
}
