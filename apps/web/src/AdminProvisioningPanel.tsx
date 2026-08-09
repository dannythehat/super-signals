import { FormEvent, useState } from 'react';

type Notice = { tone: 'error' | 'success'; message: string } | null;

interface SetupResponse {
  email: string;
  display_name: string;
  role: 'trading_admin';
  setup_token: string;
  expires_at: string;
}

interface AdminProvisioningPanelProps {
  apiBaseUrl: string;
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : 'Administrator setup could not be created.';
    const message =
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : String(detail);
    throw new Error(message);
  }
  return body;
}

export function AdminProvisioningPanel({ apiBaseUrl }: AdminProvisioningPanelProps) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);
  const [setupLink, setSetupLink] = useState('');

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    setSetupLink('');
    const form = new FormData(event.currentTarget);

    try {
      const response = await fetch(`${apiBaseUrl}/admin/accounts/trading-admins/setup`, {
        method: 'POST',
        credentials: 'include',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({
          email: form.get('email'),
          display_name: form.get('display_name'),
        }),
      });
      const setup = await readJson<SetupResponse>(response);
      const link = `${window.location.origin}${window.location.pathname}#admin-setup=${encodeURIComponent(setup.setup_token)}`;
      setSetupLink(link);
      setNotice({
        tone: 'success',
        message: `${setup.display_name} is whitelisted as Trading Admin. Send the one-time setup link below privately.`,
      });
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Administrator setup could not be created.',
      });
    } finally {
      setBusy(false);
    }
  }

  async function copySetupLink() {
    if (!setupLink) return;
    try {
      await navigator.clipboard.writeText(setupLink);
      setNotice({ tone: 'success', message: 'Setup link copied. Send it directly to the Trading Admin.' });
    } catch {
      setNotice({ tone: 'error', message: 'Copy failed. Press and hold the link to copy it manually.' });
    }
  }

  return (
    <section className="admin-provisioning" aria-labelledby="admin-provisioning-title">
      <div>
        <span className="status-label">Owner only</span>
        <h2 id="admin-provisioning-title">Add a Trading Admin</h2>
        <p className="intro">
          Whitelist an administrator email and create a single-use setup link. The administrator chooses their own password.
        </p>
      </div>

      {notice && (
        <div className={`notice notice--${notice.tone}`} role="status">
          {notice.message}
        </div>
      )}

      <form className="auth-form" onSubmit={handleSubmit}>
        <label>
          Name
          <input name="display_name" type="text" defaultValue="Rikke" maxLength={120} required />
        </label>
        <label>
          Email
          <input name="email" type="email" defaultValue="rikkevenoebo@hotmail.com" autoComplete="email" required />
        </label>
        <button className="button" type="submit" disabled={busy}>
          {busy ? 'Creating setup link…' : 'Whitelist & create setup link'}
        </button>
      </form>

      {setupLink && (
        <div className="admin-setup-link">
          <label>
            One-time setup link
            <textarea readOnly rows={4} value={setupLink} onFocus={(event) => event.currentTarget.select()} />
          </label>
          <button className="button button--quiet" type="button" onClick={() => void copySetupLink()}>
            Copy setup link
          </button>
          <small>The link expires after two hours and becomes unusable after Rikke sets her password.</small>
        </div>
      )}
    </section>
  );
}
