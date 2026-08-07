import { FormEvent, useState } from 'react';

interface TelegramAccount {
  id: string;
  label: string;
  phone_hint: string;
  status: string;
  last_connected_at: string | null;
}

interface StartResponse {
  flow_id: string;
  status: 'code_required';
  phone_hint: string;
  expires_at: string;
}

interface CodeResponse {
  flow_id: string;
  status: 'pending' | 'password_required' | 'connected' | 'expired';
  account: TelegramAccount | null;
}

interface Props {
  apiBaseUrl: string;
  disabled: boolean;
  onConnected: (account: TelegramAccount) => void;
  onPasswordRequired: (flowId: string) => void;
  onNotice: (tone: 'error' | 'success', message: string) => void;
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

export function TelegramPhoneAuthorization({
  apiBaseUrl,
  disabled,
  onConnected,
  onPasswordRequired,
  onNotice,
}: Props) {
  const [authorization, setAuthorization] = useState<StartResponse | null>(null);
  const [busy, setBusy] = useState(false);

  async function begin(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const response = await fetch(`${apiBaseUrl}/admin/telegram/accounts/authorize/code`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          label: form.get('label'),
          phone_number: form.get('phone_number'),
        }),
      });
      const started = await readJson<StartResponse>(response);
      setAuthorization(started);
      onNotice('success', 'Telegram sent a one-time login code. Open Telegram, then return here.');
    } catch (error) {
      onNotice(
        'error',
        error instanceof Error ? error.message : 'Telegram login code could not be sent.',
      );
    } finally {
      setBusy(false);
    }
  }

  async function submitCode(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!authorization) return;
    const form = new FormData(event.currentTarget);
    setBusy(true);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/accounts/authorize/${authorization.flow_id}/code`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ code: form.get('code') }),
        },
      );
      const result = await readJson<CodeResponse>(response);
      if (result.status === 'connected' && result.account) {
        onConnected(result.account);
        return;
      }
      if (result.status === 'password_required') {
        onPasswordRequired(authorization.flow_id);
        return;
      }
      if (result.status === 'expired') {
        setAuthorization(null);
        onNotice('error', 'The Telegram login expired. Send a new code and try again.');
        return;
      }
      onNotice('error', 'Telegram did not complete the login.');
    } catch (error) {
      onNotice(
        'error',
        error instanceof Error ? error.message : 'Telegram login code was rejected.',
      );
    } finally {
      setBusy(false);
    }
  }

  if (authorization) {
    return (
      <article className="telegram-qr-card">
        <div className="telegram-qr-card__instructions" style={{ gridColumn: '1 / -1' }}>
          <span className="status-label">Same-phone login</span>
          <h3>Enter the Telegram code</h3>
          <p>
            Telegram sent a code to the account shown as <strong>{authorization.phone_hint}</strong>
            . Open Telegram, read the code, then return here.
          </p>
          <form className="telegram-password-form" onSubmit={submitCode}>
            <label>
              Telegram login code
              <input
                name="code"
                type="text"
                inputMode="numeric"
                autoComplete="one-time-code"
                maxLength={32}
                required
              />
            </label>
            <button className="button" type="submit" disabled={disabled || busy}>
              {busy ? 'Checking code…' : 'Continue'}
            </button>
            <small>The one-time code is sent directly to Telegram and is never stored.</small>
          </form>
        </div>
      </article>
    );
  }

  return (
    <>
      <form className="telegram-connect-form" onSubmit={begin}>
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
        <label>
          Telegram phone number
          <input
            name="phone_number"
            type="tel"
            autoComplete="tel"
            placeholder="International format with country code"
            required
          />
        </label>
        <button className="button" type="submit" disabled={disabled || busy}>
          {busy ? 'Sending code…' : 'Send Telegram code'}
        </button>
      </form>
      <p className="security-copy">
        The login code and any two-step verification password are never stored.
      </p>
    </>
  );
}
