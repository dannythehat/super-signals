import { FormEvent, useState } from 'react';

type Notice = { tone: 'error' | 'success'; message: string } | null;

interface AdminSetupFormProps {
  apiBaseUrl: string;
  token: string;
  onComplete: (email: string) => void;
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : 'Administrator setup could not be completed.';
    const message =
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : String(detail);
    throw new Error(message);
  }
  return body;
}

export function AdminSetupForm({ apiBaseUrl, token, onComplete }: AdminSetupFormProps) {
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);

  async function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setBusy(true);
    setNotice(null);
    const form = new FormData(event.currentTarget);
    const password = String(form.get('password') ?? '');
    const confirmPassword = String(form.get('confirm_password') ?? '');

    if (password !== confirmPassword) {
      setNotice({ tone: 'error', message: 'The two passwords do not match.' });
      setBusy(false);
      return;
    }

    try {
      const response = await fetch(`${apiBaseUrl}/auth/admin-setup`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
        body: JSON.stringify({ token, password }),
      });
      const result = await readJson<{ message: string; email: string }>(response);
      onComplete(result.email);
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Administrator setup could not be completed.',
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="app-shell">
      <section className="auth-card" aria-labelledby="admin-setup-title">
        <img className="brand-logo" src="/super-signals-logo.png" alt="Super Signals" />
        <p className="eyebrow">Trading Admin setup</p>
        <h1 id="admin-setup-title">Create your password</h1>
        <p className="intro">
          This is a one-time administrator setup link. Choose a private password of at least 12 characters.
        </p>

        {notice && (
          <div className={`notice notice--${notice.tone}`} role="status">
            {notice.message}
          </div>
        )}

        <form className="auth-form" onSubmit={handleSubmit}>
          <label>
            New password
            <input name="password" type="password" minLength={12} maxLength={128} autoComplete="new-password" required />
          </label>
          <label>
            Confirm password
            <input name="confirm_password" type="password" minLength={12} maxLength={128} autoComplete="new-password" required />
          </label>
          <button className="button" type="submit" disabled={busy}>
            {busy ? 'Securing account…' : 'Create password'}
          </button>
        </form>
      </section>
    </main>
  );
}
