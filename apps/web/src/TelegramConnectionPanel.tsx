import { FormEvent, useCallback, useEffect, useState } from 'react';

type TelegramAuthorizationStatus = 'pending' | 'password_required' | 'connected' | 'expired';

type PanelNotice = { tone: 'error' | 'success'; message: string } | null;

interface TelegramAccount {
  id: string;
  label: string;
  phone_hint: string;
  status: string;
  last_connected_at: string | null;
}

interface TelegramAuthorizationStart {
  flow_id: string;
  status: 'pending';
  qr_url: string;
  qr_image_data_uri: string;
  expires_at: string;
}

interface TelegramAuthorizationPoll {
  flow_id: string;
  status: TelegramAuthorizationStatus;
  account: TelegramAccount | null;
}

type ActiveAuthorization = Omit<TelegramAuthorizationStart, 'status'> & {
  status: TelegramAuthorizationStatus;
};

interface TelegramDisconnectResult {
  disconnected: boolean;
  server_session_destroyed: boolean;
  remote_logout: boolean;
}

interface TelegramConnectionPanelProps {
  apiBaseUrl: string;
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : 'Telegram could not complete the request.';
    const message =
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : String(detail);
    throw new Error(message);
  }
  return body;
}

function formatDate(value: string | null): string {
  if (!value) return 'Not yet verified';
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}

export function TelegramConnectionPanel({ apiBaseUrl }: TelegramConnectionPanelProps) {
  const [expanded, setExpanded] = useState(false);
  const [accounts, setAccounts] = useState<TelegramAccount[]>([]);
  const [authorization, setAuthorization] = useState<ActiveAuthorization | null>(null);
  const [notice, setNotice] = useState<PanelNotice>(null);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [disconnectConfirmation, setDisconnectConfirmation] = useState<string | null>(null);

  const loadAccounts = useCallback(async () => {
    const response = await fetch(`${apiBaseUrl}/admin/telegram/accounts`, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
    setAccounts(await readJson<TelegramAccount[]>(response));
  }, [apiBaseUrl]);

  const pollAuthorization = useCallback(async () => {
    if (!authorization || busyAction) return;
    setBusyAction('poll');
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/accounts/authorize/${authorization.flow_id}`,
        {
          credentials: 'include',
          headers: { Accept: 'application/json' },
        },
      );
      const result = await readJson<TelegramAuthorizationPoll>(response);
      if (result.status === 'connected') {
        setAuthorization(null);
        setNotice({
          tone: 'success',
          message: 'Telegram connected and the encrypted server session was saved.',
        });
        await loadAccounts();
        return;
      }
      if (result.status === 'expired') {
        setAuthorization(null);
        setNotice({
          tone: 'error',
          message: 'The Telegram QR expired. Create a new secure QR to try again.',
        });
        return;
      }
      setAuthorization((current) => (current ? { ...current, status: result.status } : current));
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram status check failed.',
      });
    } finally {
      setBusyAction(null);
    }
  }, [apiBaseUrl, authorization, busyAction, loadAccounts]);

  useEffect(() => {
    if (!authorization || authorization.status !== 'pending' || busyAction) return;
    const timer = window.setTimeout(() => {
      void pollAuthorization();
    }, 2000);
    return () => window.clearTimeout(timer);
  }, [authorization, busyAction, pollAuthorization]);

  async function handleExpand() {
    setExpanded(true);
    setBusyAction('load');
    setNotice(null);
    try {
      await loadAccounts();
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram accounts could not be loaded.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function handleBeginAuthorization(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const formElement = event.currentTarget;
    setBusyAction('begin');
    setNotice(null);
    const form = new FormData(formElement);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/telegram/accounts/authorize`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ label: form.get('label') }),
      });
      const started = await readJson<TelegramAuthorizationStart>(response);
      formElement.reset();
      setAuthorization(started);
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram QR creation failed.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function handleTelegramPassword(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!authorization) return;
    const formElement = event.currentTarget;
    setBusyAction('password');
    setNotice(null);
    const form = new FormData(formElement);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/accounts/authorize/${authorization.flow_id}/password`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ password: form.get('password') }),
        },
      );
      const result = await readJson<TelegramAuthorizationPoll>(response);
      if (result.status !== 'connected') {
        throw new Error('Telegram did not complete the protected sign-in.');
      }
      formElement.reset();
      setAuthorization(null);
      setNotice({
        tone: 'success',
        message: 'Telegram connected and the encrypted server session was saved.',
      });
      await loadAccounts();
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram sign-in failed.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function handleVerify(accountId: string) {
    setBusyAction(`verify:${accountId}`);
    setNotice(null);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/telegram/accounts/${accountId}/verify`, {
        method: 'POST',
        credentials: 'include',
        headers: { Accept: 'application/json' },
      });
      await readJson<TelegramAccount>(response);
      setNotice({
        tone: 'success',
        message: 'The encrypted Telegram session survived restart and is still authorised.',
      });
      await loadAccounts();
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram verification failed.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function handleDisconnect(accountId: string) {
    if (disconnectConfirmation !== accountId) {
      setDisconnectConfirmation(accountId);
      return;
    }

    setBusyAction(`disconnect:${accountId}`);
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/accounts/${accountId}/disconnect`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { Accept: 'application/json' },
        },
      );
      const result = await readJson<TelegramDisconnectResult>(response);
      setDisconnectConfirmation(null);
      setNotice({
        tone: 'success',
        message: result.remote_logout
          ? 'Telegram logged out remotely and the saved server session was destroyed.'
          : 'The saved server session was destroyed. Telegram could not confirm remote logout.',
      });
      await loadAccounts();
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram disconnect failed.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <section className="telegram-panel" aria-labelledby="telegram-panel-title">
      <div className="telegram-panel__header">
        <div>
          <span className="status-label">Secure source access</span>
          <h2 id="telegram-panel-title">Telegram connection</h2>
          <p>
            Connect an administrator-controlled reader account. Signal providers remain hidden from
            invited users.
          </p>
        </div>
        {!expanded && (
          <button className="button button--quiet" type="button" onClick={handleExpand}>
            Manage Telegram accounts
          </button>
        )}
      </div>

      {expanded && (
        <div className="telegram-panel__body">
          {notice && (
            <div className={`notice notice--${notice.tone}`} role="status">
              {notice.message}
            </div>
          )}

          <div className="telegram-account-list" aria-label="Connected Telegram accounts">
            {busyAction === 'load' && <p className="muted-copy">Loading secure accounts…</p>}
            {!busyAction && accounts.length === 0 && (
              <p className="muted-copy">No Telegram reader account is connected yet.</p>
            )}
            {accounts.map((telegramAccount) => (
              <article className="telegram-account" key={telegramAccount.id}>
                <div>
                  <span
                    className={`connection-status connection-status--${telegramAccount.status}`}
                  >
                    {telegramAccount.status}
                  </span>
                  <h3>{telegramAccount.label}</h3>
                  <p>{telegramAccount.phone_hint}</p>
                  <small>Last verified: {formatDate(telegramAccount.last_connected_at)}</small>
                </div>
                <div className="telegram-account__actions">
                  {telegramAccount.status === 'connected' && (
                    <button
                      className="button button--quiet"
                      type="button"
                      onClick={() => void handleVerify(telegramAccount.id)}
                      disabled={busyAction !== null}
                    >
                      {busyAction === `verify:${telegramAccount.id}` ? 'Verifying…' : 'Verify'}
                    </button>
                  )}
                  {telegramAccount.status !== 'disconnected' && (
                    <button
                      className="button button--danger"
                      type="button"
                      onClick={() => void handleDisconnect(telegramAccount.id)}
                      disabled={busyAction !== null}
                    >
                      {busyAction === `disconnect:${telegramAccount.id}`
                        ? 'Disconnecting…'
                        : disconnectConfirmation === telegramAccount.id
                          ? 'Confirm disconnect'
                          : 'Disconnect'}
                    </button>
                  )}
                </div>
              </article>
            ))}
          </div>

          {authorization ? (
            <article className="telegram-qr-card" aria-live="polite">
              <div className="telegram-qr-card__image">
                <img src={authorization.qr_image_data_uri} alt="Telegram authorisation QR code" />
              </div>
              <div className="telegram-qr-card__instructions">
                <span className="status-label">Short-lived authorisation</span>
                <h3>Scan with Telegram</h3>
                <ol>
                  <li>Open Telegram on an already authorised device.</li>
                  <li>Open Devices, then Link Desktop Device.</li>
                  <li>Scan this QR before {formatDate(authorization.expires_at)}.</li>
                </ol>
                <a className="button telegram-open-link" href={authorization.qr_url}>
                  Open in Telegram
                </a>
                <button
                  className="button button--quiet"
                  type="button"
                  onClick={() => void pollAuthorization()}
                  disabled={busyAction !== null}
                >
                  {busyAction === 'poll' ? 'Checking…' : 'Check connection'}
                </button>
                <p className="security-copy">
                  This QR is never saved to the database or audit log and expires automatically.
                </p>
              </div>

              {authorization.status === 'password_required' && (
                <form className="telegram-password-form" onSubmit={handleTelegramPassword}>
                  <label>
                    Telegram two-step password
                    <input
                      name="password"
                      type="password"
                      autoComplete="current-password"
                      required
                    />
                  </label>
                  <button className="button" type="submit" disabled={busyAction !== null}>
                    {busyAction === 'password' ? 'Authorising…' : 'Complete protected sign-in'}
                  </button>
                  <small>The password is sent directly to Telegram and is never stored.</small>
                </form>
              )}
            </article>
          ) : (
            <form className="telegram-connect-form" onSubmit={handleBeginAuthorization}>
              <label>
                Private account label
                <input
                  name="label"
                  type="text"
                  maxLength={80}
                  placeholder="Primary signal reader"
                  required
                />
              </label>
              <button className="button" type="submit" disabled={busyAction !== null}>
                {busyAction === 'begin' ? 'Creating secure QR…' : 'Create secure QR'}
              </button>
            </form>
          )}
        </div>
      )}
    </section>
  );
}
