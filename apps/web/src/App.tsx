import { useEffect, useState } from 'react';

import { APP_NAME, BUILD_PHASE, type HealthResponse } from '@super-signals/shared';

type ConnectionState = 'checking' | 'healthy' | 'unavailable';

const apiBaseUrl = import.meta.env.VITE_API_BASE_URL ?? '/api';

export function App() {
  const [connection, setConnection] = useState<ConnectionState>('checking');
  const [health, setHealth] = useState<HealthResponse | null>(null);

  useEffect(() => {
    const controller = new AbortController();

    async function checkApi() {
      try {
        const response = await fetch(`${apiBaseUrl}/health`, {
          headers: { Accept: 'application/json' },
          signal: controller.signal,
        });

        if (!response.ok) {
          throw new Error(`Health check failed with status ${response.status}`);
        }

        const result = (await response.json()) as HealthResponse;
        setHealth(result);
        setConnection('healthy');
      } catch (error) {
        if (error instanceof DOMException && error.name === 'AbortError') {
          return;
        }

        setConnection('unavailable');
      }
    }

    void checkApi();

    return () => controller.abort();
  }, []);

  const connectionLabel = {
    checking: 'Checking API',
    healthy: 'API connected',
    unavailable: 'API unavailable',
  }[connection];

  return (
    <main className="app-shell">
      <section className="hero-card" aria-labelledby="app-title">
        <div className="brand-mark" aria-hidden="true">
          SS
        </div>

        <p className="eyebrow">Private trading infrastructure</p>
        <h1 id="app-title">{APP_NAME}</h1>
        <p className="intro">
          The secure foundation for approved Telegram signals, controlled automation and Vantage MT5
          execution.
        </p>

        <div className="status-grid" aria-label="Build status">
          <article className="status-card">
            <span className="status-label">Build phase</span>
            <strong>{BUILD_PHASE}</strong>
            <small>Frontend scaffold running</small>
          </article>

          <article className={`status-card status-card--${connection}`}>
            <span className="status-label">Backend</span>
            <strong>{connectionLabel}</strong>
            <small>
              {health ? `${health.service} v${health.version}` : 'FastAPI health endpoint'}
            </small>
          </article>
        </div>

        <div className="foundation-note">
          <span className="pulse" aria-hidden="true" />
          Demo foundation only. No live trading is enabled.
        </div>
      </section>
    </main>
  );
}
