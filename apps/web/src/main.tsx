import React from 'react';
import ReactDOM from 'react-dom/client';

import { App } from './App';
import './styles.css';
import './navigation.css';
import './workspace-enhancements.css';
import './onboarding.css';

ReactDOM.createRoot(document.getElementById('root')!).render(
  <React.StrictMode>
    <App />
  </React.StrictMode>,
);
