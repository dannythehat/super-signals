import { useEffect, useState } from 'react';

interface PushNotificationsDay34Props {
  apiBaseUrl: string;
}

interface PushConfig {
  enabled: boolean;
  public_key: string | null;
}

type PushState = 'checking' | 'unsupported' | 'unavailable' | 'off' | 'on' | 'blocked';
type EnablePhase = 'permission' | 'service-worker' | 'subscription' | 'server' | null;

function urlBase64ToUint8Array(value: string): Uint8Array {
  const padding = '='.repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replace(/-/g, '+').replace(/_/g, '/');
  const raw = window.atob(base64);
  return Uint8Array.from(raw, (character) => character.charCodeAt(0));
}

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, message: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => reject(new Error(message)), timeoutMs);
    promise.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      },
    );
  });
}

async function ensureServiceWorker(): Promise<ServiceWorkerRegistration> {
  const registration = await withTimeout(
    navigator.serviceWorker.register('/sw.js', { scope: '/' }),
    10000,
    'The notification service could not start in this browser. Open Super Signals directly in Chrome and try again.',
  );
  await withTimeout(
    registration.update(),
    10000,
    'The notification service could not update in this browser. Open Super Signals directly in Chrome and try again.',
  );
  return withTimeout(
    navigator.serviceWorker.ready,
    10000,
    'The notification service did not become ready. Open Super Signals directly in Chrome and try again.',
  );
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : 'Trade alerts could not be updated.';
    const message =
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : String(detail);
    throw new Error(message);
  }
  return body;
}

export function PushNotificationsDay34({ apiBaseUrl }: PushNotificationsDay34Props) {
  const [state, setState] = useState<PushState>('checking');
  const [busy, setBusy] = useState(false);
  const [phase, setPhase] = useState<EnablePhase>(null);
  const [message, setMessage] = useState<string | null>(null);
  const [publicKey, setPublicKey] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    async function check() {
      if (!('serviceWorker' in navigator) || !('PushManager' in window) || !('Notification' in window)) {
        if (!cancelled) setState('unsupported');
        return;
      }
      try {
        const response = await fetch(`${apiBaseUrl}/notifications/push/config`, {
          credentials: 'include',
          headers: { Accept: 'application/json' },
        });
        const config = await readJson<PushConfig>(response);
        if (cancelled) return;
        if (!config.enabled || !config.public_key) {
          setState('unavailable');
          return;
        }
        setPublicKey(config.public_key);
        if (Notification.permission === 'denied') {
          setState('blocked');
          return;
        }
        const registration = await ensureServiceWorker();
        const subscription = await withTimeout(
          registration.pushManager.getSubscription(),
          10000,
          'The browser could not read the notification subscription.',
        );
        if (!cancelled) setState(subscription ? 'on' : 'off');
      } catch (error) {
        if (!cancelled) {
          setState('unavailable');
          setMessage(error instanceof Error ? error.message : 'Trade alerts are not available in this browser.');
        }
      }
    }
    void check();
    return () => {
      cancelled = true;
    };
  }, [apiBaseUrl]);

  async function enable() {
    if (!publicKey) return;
    setBusy(true);
    setMessage(null);
    try {
      setPhase('permission');
      const permission = await withTimeout(
        Notification.requestPermission(),
        15000,
        'Your browser did not open the notification permission prompt. Open Super Signals directly in Chrome and try again.',
      );
      if (permission !== 'granted') {
        setState(permission === 'denied' ? 'blocked' : 'off');
        setMessage('Trade alerts were not enabled. You can change this later.');
        return;
      }

      setPhase('service-worker');
      const registration = await ensureServiceWorker();
      let subscription = await withTimeout(
        registration.pushManager.getSubscription(),
        10000,
        'The browser could not read its notification subscription.',
      );
      if (!subscription) {
        setPhase('subscription');
        subscription = await withTimeout(
          registration.pushManager.subscribe({
            userVisibleOnly: true,
            applicationServerKey: urlBase64ToUint8Array(publicKey),
          }),
          20000,
          'This browser could not create a push subscription. Open Super Signals directly in Chrome and try again.',
        );
      }

      setPhase('server');
      const response = await withTimeout(
        fetch(`${apiBaseUrl}/notifications/push/subscribe`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify(subscription.toJSON()),
        }),
        15000,
        'Super Signals could not save this device notification subscription.',
      );
      await readJson<{ enabled: boolean }>(response);
      setState('on');
      setMessage('Trade alerts are on for this device.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Trade alerts could not be enabled.');
    } finally {
      setPhase(null);
      setBusy(false);
    }
  }

  async function disable() {
    setBusy(true);
    setMessage(null);
    try {
      const registration = await ensureServiceWorker();
      const subscription = await withTimeout(
        registration.pushManager.getSubscription(),
        10000,
        'The browser could not read its notification subscription.',
      );
      if (subscription) {
        const response = await fetch(`${apiBaseUrl}/notifications/push/unsubscribe`, {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ endpoint: subscription.endpoint }),
        });
        await readJson<{ disabled: boolean }>(response);
        await subscription.unsubscribe();
      }
      setState('off');
      setMessage('Trade alerts are off for this device.');
    } catch (error) {
      setMessage(error instanceof Error ? error.message : 'Trade alerts could not be disabled.');
    } finally {
      setBusy(false);
    }
  }

  const statusLabel =
    state === 'on'
      ? 'On'
      : state === 'off'
        ? 'Off'
        : state === 'blocked'
          ? 'Blocked in browser'
          : state === 'unsupported'
            ? 'Not supported'
            : state === 'unavailable'
              ? 'Not available yet'
              : 'Checking…';

  const busyLabel =
    phase === 'permission'
      ? 'Waiting for permission…'
      : phase === 'service-worker'
        ? 'Starting notifications…'
        : phase === 'subscription'
          ? 'Registering this device…'
          : phase === 'server'
            ? 'Saving this device…'
            : 'Turning on…';

  return (
    <article className="settings-card" aria-labelledby="trade-alerts-heading">
      <span className="status-label">Device notifications · {statusLabel}</span>
      <h2 id="trade-alerts-heading">Trade alerts</h2>
      <p>
        Get plain-English Super Signals updates on this device when a trade opens, changes or closes.
        Private balances and account details are never included in push alerts.
      </p>
      {busy && phase && <p className="form-note" role="status">{busyLabel}</p>}
      {state === 'blocked' && (
        <p className="form-note">Notifications are blocked in your browser settings for this site.</p>
      )}
      {message && <p className="form-note" role="status">{message}</p>}
      <div className="settings-actions">
        {state === 'off' && (
          <button className="button" type="button" onClick={() => void enable()} disabled={busy}>
            {busy ? busyLabel : 'Turn on trade alerts'}
          </button>
        )}
        {state === 'on' && (
          <button className="button button--quiet" type="button" onClick={() => void disable()} disabled={busy}>
            {busy ? 'Turning off…' : 'Turn off alerts'}
          </button>
        )}
      </div>
    </article>
  );
}
