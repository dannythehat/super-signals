import { useCallback, useEffect, useMemo, useState } from 'react';

import { TelegramConnectionPanel } from './TelegramConnectionPanel';

type Notice = { tone: 'error' | 'success'; message: string } | null;

interface TelegramAccount {
  id: string;
  label: string;
  phone_hint: string;
  status: string;
}

interface TelegramSelectableSource {
  chat_id: number;
  title: string;
  kind: 'group' | 'channel';
  selected: boolean;
  source_id: string | null;
  status: string | null;
  managed_by_this_reader?: boolean;
}

interface TradingAdminOnboardingProps {
  apiBaseUrl: string;
  displayName: string;
  onComplete: () => void;
}

async function readJson<T>(response: Response): Promise<T> {
  const body = (await response.json()) as T;
  if (!response.ok) {
    const detail =
      typeof body === 'object' && body !== null && 'detail' in body
        ? (body as { detail: unknown }).detail
        : 'Super Signals could not complete the request.';
    const message =
      typeof detail === 'object' && detail !== null && 'message' in detail
        ? String((detail as { message: unknown }).message)
        : String(detail);
    throw new Error(message);
  }
  return body;
}

export function TradingAdminOnboarding({
  apiBaseUrl,
  displayName,
  onComplete,
}: TradingAdminOnboardingProps) {
  const [account, setAccount] = useState<TelegramAccount | null>(null);
  const [sources, setSources] = useState<TelegramSelectableSource[]>([]);
  const [checked, setChecked] = useState<Set<number>>(new Set());
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<Notice>(null);

  const loadSetup = useCallback(async () => {
    setLoading(true);
    setNotice(null);
    try {
      const accountsResponse = await fetch(`${apiBaseUrl}/admin/telegram/accounts`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
      });
      const accounts = await readJson<TelegramAccount[]>(accountsResponse);
      const connected = accounts.find((item) => item.status === 'connected') ?? null;
      setAccount(connected);

      if (!connected) {
        setSources([]);
        return;
      }

      const sourcesResponse = await fetch(
        `${apiBaseUrl}/admin/telegram/sources/accounts/${connected.id}/available`,
        {
          credentials: 'include',
          headers: { Accept: 'application/json' },
        },
      );
      const available = await readJson<TelegramSelectableSource[]>(sourcesResponse);
      setSources(
        available.filter((source) => source.kind === 'group' || source.kind === 'channel'),
      );
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Setup could not be loaded.',
      });
    } finally {
      setLoading(false);
    }
  }, [apiBaseUrl]);

  useEffect(() => {
    void loadSetup();
  }, [loadSetup]);

  const alreadyAdded = useMemo(
    () => sources.filter((source) => source.selected && source.managed_by_this_reader !== false),
    [sources],
  );
  const availableToAdd = useMemo(
    () => sources.filter((source) => !source.selected || source.managed_by_this_reader === false),
    [sources],
  );

  function toggle(chatId: number) {
    setChecked((current) => {
      const next = new Set(current);
      if (next.has(chatId)) next.delete(chatId);
      else next.add(chatId);
      return next;
    });
  }

  async function addSelectedGroups() {
    if (!account || checked.size === 0) return;
    setBusy(true);
    setNotice(null);
    try {
      const targets = availableToAdd.filter((source) => checked.has(source.chat_id));
      let added = 0;

      for (const source of targets) {
        const selectResponse = await fetch(
          `${apiBaseUrl}/admin/telegram/sources/accounts/${account.id}/select`,
          {
            method: 'POST',
            credentials: 'include',
            headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
            body: JSON.stringify({ chat_id: source.chat_id }),
          },
        );
        const linked = await readJson<TelegramSelectableSource>(selectResponse);

        // Rikke does not need to understand operational states during onboarding.
        // TESTING enables the Day 12 read-only listener but still cannot execute trades.
        if (linked.source_id && (linked.status ?? 'paused') === 'paused') {
          const statusResponse = await fetch(
            `${apiBaseUrl}/admin/telegram/sources/shared/${linked.source_id}/status`,
            {
              method: 'PATCH',
              credentials: 'include',
              headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
              body: JSON.stringify({ status: 'testing' }),
            },
          );
          await readJson(statusResponse);
        }
        added += 1;
      }

      setChecked(new Set());
      await loadSetup();
      setNotice({
        tone: 'success',
        message: `${added} ${added === 1 ? 'group' : 'groups'} added. Super Signals can now read new messages from them in TESTING mode. Live trading is still disabled.`,
      });
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'The selected groups could not be added.',
      });
    } finally {
      setBusy(false);
    }
  }

  return (
    <section className="admin-onboarding" aria-labelledby="admin-onboarding-title">
      <div className="admin-onboarding__hero">
        <span className="status-label">One-time setup</span>
        <h1 id="admin-onboarding-title">Hi {displayName} — connect Telegram once</h1>
        <p>
          This should only take a few minutes. Connect your Telegram account, choose the signal
          groups you supply, then you are finished. You do not need to manage servers, source
          states or trading settings.
        </p>
      </div>

      <div className="onboarding-steps" aria-label="Telegram setup progress">
        <div className={`onboarding-step ${account ? 'onboarding-step--done' : 'onboarding-step--active'}`}>
          <span>1</span>
          <div>
            <strong>Connect Telegram</strong>
            <small>{account ? 'Connected ✓' : 'Use your normal Telegram phone number.'}</small>
          </div>
        </div>
        <div className={`onboarding-step ${account ? 'onboarding-step--active' : ''}`}>
          <span>2</span>
          <div>
            <strong>Choose your signal groups</strong>
            <small>{alreadyAdded.length > 0 ? `${alreadyAdded.length} added ✓` : 'Tick the groups you supply.'}</small>
          </div>
        </div>
        <div className={`onboarding-step ${alreadyAdded.length > 0 ? 'onboarding-step--done' : ''}`}>
          <span>3</span>
          <div>
            <strong>Done</strong>
            <small>Super Signals keeps the reader connection for you.</small>
          </div>
        </div>
      </div>

      {notice && (
        <div className={`notice notice--${notice.tone}`} role="status">
          {notice.message}
        </div>
      )}

      {loading ? (
        <div className="onboarding-card">
          <p className="muted-copy">Checking your Telegram setup…</p>
        </div>
      ) : !account ? (
        <div className="onboarding-card">
          <div className="onboarding-card__intro">
            <span className="status-label">Step 1</span>
            <h2>Connect your Telegram account</h2>
            <p>
              Enter your phone number. Telegram will send you a code. If your Telegram account uses
              a two-step password, enter it when asked. Super Signals stores only the encrypted
              Telegram session — not your code or password.
            </p>
          </div>
          <TelegramConnectionPanel
            apiBaseUrl={apiBaseUrl}
            startExpanded
            onConnected={() => void loadSetup()}
          />
        </div>
      ) : (
        <div className="onboarding-card">
          <div className="onboarding-connected-reader">
            <div>
              <span className="connection-status connection-status--connected">CONNECTED ✓</span>
              <strong>{account.label}</strong>
              <small>{account.phone_hint}</small>
            </div>
            <button className="button button--quiet" type="button" onClick={() => void loadSetup()} disabled={busy}>
              Refresh groups
            </button>
          </div>

          <div className="onboarding-card__intro">
            <span className="status-label">Step 2</span>
            <h2>Choose the groups you supply</h2>
            <p>
              Tick every Telegram group or channel you want Super Signals to read. You can choose
              several and add them in one go. Newly added groups automatically enter TESTING mode,
              which reads new messages but cannot place trades.
            </p>
          </div>

          {alreadyAdded.length > 0 && (
            <div className="onboarding-added-list" aria-label="Groups already added from this reader">
              {alreadyAdded.map((source) => (
                <div className="onboarding-added-item" key={`${source.kind}:${source.chat_id}`}>
                  <span aria-hidden="true">✓</span>
                  <div>
                    <strong>{source.title}</strong>
                    <small>Added from your Telegram reader · {(source.status ?? 'paused').toUpperCase()}</small>
                  </div>
                </div>
              ))}
            </div>
          )}

          {availableToAdd.length === 0 ? (
            <div className="overview-source-empty">
              <strong>No other groups are available to add</strong>
              <small>If you just joined a Telegram group, tap Refresh groups.</small>
            </div>
          ) : (
            <div className="onboarding-source-picker" aria-label="Telegram groups available to add">
              {availableToAdd.map((source) => (
                <label className="onboarding-source-option" key={`${source.kind}:${source.chat_id}`}>
                  <input
                    type="checkbox"
                    checked={checked.has(source.chat_id)}
                    onChange={() => toggle(source.chat_id)}
                    disabled={busy}
                  />
                  <span>
                    <strong>{source.title}</strong>
                    <small>
                      {source.selected
                        ? 'Already shared in Super Signals — add your reader as backup access'
                        : source.kind === 'channel'
                          ? 'Telegram channel'
                          : 'Telegram group'}
                    </small>
                  </span>
                </label>
              ))}
            </div>
          )}

          {availableToAdd.length > 0 && (
            <button
              className="button onboarding-primary-action"
              type="button"
              onClick={() => void addSelectedGroups()}
              disabled={busy || checked.size === 0}
            >
              {busy
                ? 'Adding groups…'
                : checked.size === 0
                  ? 'Select the groups to add'
                  : `Add ${checked.size} selected ${checked.size === 1 ? 'group' : 'groups'}`}
            </button>
          )}

          {alreadyAdded.length > 0 && (
            <div className="onboarding-finish">
              <strong>That’s all you need to do.</strong>
              <p>
                Leave Telegram connected. Super Signals now has authorised reader access to the
                groups above and can capture their new messages while they are in TESTING or LIVE.
              </p>
              <button className="button" type="button" onClick={onComplete} disabled={busy}>
                Done — open Super Signals
              </button>
            </div>
          )}
        </div>
      )}
    </section>
  );
}
