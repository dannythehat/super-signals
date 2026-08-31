import { FormEvent, useState } from 'react';

interface ImportedSource {
  chat_id: number;
  title: string;
  kind: 'group' | 'channel';
  selected: boolean;
  source_id: string | null;
  status: string | null;
}

interface ProviderLabImportProps {
  apiBaseUrl: string;
  accountId: string;
  disabled?: boolean;
  onImported: (source: ImportedSource) => Promise<void> | void;
}

const RESEARCH_CANDIDATES = [
  {
    name: 'GoldSignals.io',
    identifier: '@GoldSignalsOfficial',
    note: 'Gold-focused public feed with XAUUSD reporting and TP/SL management language.',
  },
  {
    name: 'Gold Signals VIP',
    identifier: '@Gold_Signals',
    note: 'Public XAUUSD feed with explicit entry, stop and take-profit structures.',
  },
  {
    name: 'XAUUSD SIGNALS',
    identifier: '@XAUUSDSIGNALSG1',
    note: 'Gold-only style with multi-TP signals and TP-hit follow-up posts.',
  },
  {
    name: 'XAUUSD Scalping Signals',
    identifier: '@xauusd_trading_scalping_signals',
    note: 'Scalping feed with multiple targets, BE instructions and partial-profit management.',
  },
  {
    name: 'XAUUSD Gold Scalping Signals',
    identifier: '@Xauusd_Gold_Scalping_Signal',
    note: 'Short-distance multi-TP Gold signals with close/partial instructions.',
  },
  {
    name: 'XAUUSD/GOLD SIGNALS',
    identifier: '@XAUUSDGOLDFXPIPS',
    note: 'Entry zones, up to four targets and explicit stop losses.',
  },
  {
    name: 'Gold FX Signals',
    identifier: '@gold_fx_signals_001',
    note: 'Entry ranges, six-target ladders and stated risk per trade.',
  },
  {
    name: 'XAUUSD Adviser',
    identifier: '@redanalyze',
    note: 'Wide target ladders useful for testing parser and TP-allocation behaviour.',
  },
  {
    name: 'Gold Trader Sunny',
    identifier: '@goldtradersunny',
    note: 'Large Gold-focused feed using entry zones, multiple targets and re-entry language.',
  },
];

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : 'Provider Lab could not complete the request.';
    const message =
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : String(detail);
    throw new Error(message);
  }
  return body;
}

export function ProviderLabImport({
  apiBaseUrl,
  accountId,
  disabled = false,
  onImported,
}: ProviderLabImportProps) {
  const [identifier, setIdentifier] = useState('');
  const [busyIdentifier, setBusyIdentifier] = useState<string | null>(null);
  const [notice, setNotice] = useState<{ tone: 'success' | 'error'; message: string } | null>(null);

  async function importProvider(value: string) {
    const clean = value.trim();
    if (!accountId || !clean) return;
    setBusyIdentifier(clean);
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/sources/accounts/${accountId}/import-public`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ identifier: clean }),
        },
      );
      const imported = await readJson<ImportedSource>(response);
      setIdentifier('');
      setNotice({
        tone: 'success',
        message:
          imported.status === 'shadow'
            ? `${imported.title} is now in SHADOW research. It can be read and simulated, but cannot open MT5 positions.`
            : `${imported.title} is already catalogued as ${String(imported.status ?? 'paused').toUpperCase()}. No live state was created by Provider Lab.`,
      });
      await onImported(imported);
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Public provider could not be imported.',
      });
    } finally {
      setBusyIdentifier(null);
    }
  }

  function handleSubmit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    void importProvider(identifier);
  }

  return (
    <section className="owner-alerts" aria-labelledby="provider-lab-import-title">
      <div className="owner-alerts__header">
        <span className="status-label">Provider Lab</span>
        <h3 id="provider-lab-import-title">Add public Gold providers</h3>
      </div>
      <p className="muted-copy">
        Paste a public @username or t.me link. Provider Lab joins it through this private reader and
        creates new research sources as SHADOW only. Private invite links are rejected.
      </p>

      {notice && (
        <div className={`notice notice--${notice.tone}`} role="status">
          {notice.message}
        </div>
      )}

      <form className="auth-form" onSubmit={handleSubmit}>
        <label>
          Public Telegram provider
          <input
            value={identifier}
            onChange={(event) => setIdentifier(event.currentTarget.value)}
            placeholder="@GoldSignalsOfficial or https://t.me/GoldSignalsOfficial"
            autoComplete="off"
            disabled={disabled || busyIdentifier !== null}
          />
        </label>
        <button
          className="button"
          type="submit"
          disabled={disabled || busyIdentifier !== null || !identifier.trim()}
        >
          {busyIdentifier === identifier.trim() && busyIdentifier !== null
            ? 'Importing to Shadow…'
            : 'Import to Shadow'}
        </button>
      </form>

      <div>
        <span className="status-label">Verified public starter pool</span>
        <div className="telegram-account-list" aria-label="Provider Lab starter candidates">
          {RESEARCH_CANDIDATES.map((candidate) => (
            <article className="telegram-account" key={candidate.identifier}>
              <div>
                <span className="connection-status">RESEARCH CANDIDATE</span>
                <h3>{candidate.name}</h3>
                <small>{candidate.identifier}</small>
                <small>{candidate.note}</small>
              </div>
              <div className="telegram-account__actions">
                <button
                  className="button button--quiet"
                  type="button"
                  onClick={() => void importProvider(candidate.identifier)}
                  disabled={disabled || busyIdentifier !== null}
                >
                  {busyIdentifier === candidate.identifier ? 'Importing…' : 'Add to Shadow'}
                </button>
              </div>
            </article>
          ))}
        </div>
      </div>
    </section>
  );
}
