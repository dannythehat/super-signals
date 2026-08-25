import { useState } from 'react';

type Notice = { tone: 'error' | 'success'; message: string } | null;
type SourceState = 'testing' | 'shadow' | 'live' | 'paused';

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

interface SharedTelegramSource {
  source_id: string;
  chat_id: number;
  title: string;
  status: string;
  shadow_total: number;
  shadow_open: number;
  shadow_closed: number;
  shadow_wins: number;
  shadow_losses: number;
  shadow_return_percent: string;
}

interface SourceStatusChange {
  source_id: string;
  title: string;
  previous_status: string;
  status: SourceState;
  changed_at: string;
  actor_display_name: string;
  actor_role: string;
  monitoring_started: false;
  live_trading_enabled: false;
}

interface OwnerSourceAlert {
  event_id: number;
  source_id: string;
  title: string;
  previous_status: string;
  status: string;
  actor_display_name: string;
  changed_at: string;
}

interface TelegramSourceSelectorProps {
  apiBaseUrl: string;
  canViewOwnerAlerts?: boolean;
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

function formatAlertDate(value: string): string {
  return new Intl.DateTimeFormat(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  }).format(new Date(value));
}

export function TelegramSourceSelector({
  apiBaseUrl,
  canViewOwnerAlerts = true,
}: TelegramSourceSelectorProps) {
  const [expanded, setExpanded] = useState(false);
  const [accounts, setAccounts] = useState<TelegramAccount[]>([]);
  const [accountId, setAccountId] = useState('');
  const [sources, setSources] = useState<TelegramSelectableSource[]>([]);
  const [sharedSources, setSharedSources] = useState<SharedTelegramSource[]>([]);
  const [ownerAlerts, setOwnerAlerts] = useState<OwnerSourceAlert[] | null>(null);
  const [sharedCatalogueAvailable, setSharedCatalogueAvailable] = useState(true);
  const [loaded, setLoaded] = useState(false);
  const [busyAction, setBusyAction] = useState<string | null>(null);
  const [notice, setNotice] = useState<Notice>(null);

  async function fetchSharedSources() {
    const response = await fetch(`${apiBaseUrl}/admin/telegram/sources/shared`, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
    if (response.status === 404) {
      setSharedCatalogueAvailable(false);
      setSharedSources([]);
      return;
    }
    const shared = await readJson<SharedTelegramSource[]>(response);
    setSharedCatalogueAvailable(true);
    setSharedSources(shared);
  }

  async function fetchOwnerAlerts() {
    const response = await fetch(`${apiBaseUrl}/admin/telegram/sources/owner-alerts`, {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
    if (response.status === 403 || response.status === 404) {
      setOwnerAlerts(null);
      return;
    }
    setOwnerAlerts(await readJson<OwnerSourceAlert[]>(response));
  }

  async function handleExpand() {
    setExpanded(true);
    setBusyAction('accounts');
    setNotice(null);
    try {
      const accountsResponse = await fetch(`${apiBaseUrl}/admin/telegram/accounts`, {
        credentials: 'include',
        headers: { Accept: 'application/json' },
      });
      const allAccounts = await readJson<TelegramAccount[]>(accountsResponse);
      const connected = allAccounts.filter((account) => account.status === 'connected');
      setAccounts(connected);
      if (connected.length > 0) {
        setAccountId((current) => current || connected[0].id);
      }
      if (connected.length === 0) {
        setNotice({
          tone: 'error',
          message: 'Connect your own Telegram reader before adding another signal source.',
        });
      }
      await fetchSharedSources();
      if (canViewOwnerAlerts) {
        await fetchOwnerAlerts();
      } else {
        setOwnerAlerts(null);
      }
    } catch (error) {
      setNotice({
        tone: 'error',
        message:
          error instanceof Error ? error.message : 'Telegram source settings could not be loaded.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function loadSources(selectedAccountId = accountId) {
    if (!selectedAccountId) return;
    setBusyAction('discover');
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/sources/accounts/${selectedAccountId}/available`,
        {
          credentials: 'include',
          headers: { Accept: 'application/json' },
        },
      );
      const discovered = await readJson<TelegramSelectableSource[]>(response);
      setSources(
        discovered.filter((source) => source.kind === 'group' || source.kind === 'channel'),
      );
      setLoaded(true);
      await fetchSharedSources();
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram sources could not be loaded.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function changeSourceStatus(source: SharedTelegramSource, nextStatus: SourceState) {
    if (source.status === nextStatus) return;
    setBusyAction(`status:${source.source_id}:${nextStatus}`);
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/sources/shared/${source.source_id}/status`,
        {
          method: 'PATCH',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ status: nextStatus }),
        },
      );
      const changed = await readJson<SourceStatusChange>(response);
      setSharedSources((current) =>
        current.map((item) =>
          item.source_id === changed.source_id ? { ...item, status: changed.status } : item,
        ),
      );
      setSources((current) =>
        current.map((item) =>
          item.source_id === changed.source_id ? { ...item, status: changed.status } : item,
        ),
      );
      setNotice({
        tone: 'success',
        message: `${changed.title} moved from ${changed.previous_status.toUpperCase()} to ${changed.status.toUpperCase()}. The change was audited. Live trading remains disabled.`,
      });
      if (canViewOwnerAlerts) {
        await fetchOwnerAlerts();
      }
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Source state could not be changed.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function selectSource(source: TelegramSelectableSource) {
    if (!accountId) return;
    const wasAlreadyShared = source.selected;
    setBusyAction(`select:${source.chat_id}`);
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/sources/accounts/${accountId}/select`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { 'Content-Type': 'application/json', Accept: 'application/json' },
          body: JSON.stringify({ chat_id: source.chat_id }),
        },
      );
      await readJson<TelegramSelectableSource>(response);
      setNotice({
        tone: 'success',
        message: !sharedCatalogueAvailable
          ? 'Source selected and kept PAUSED.'
          : wasAlreadyShared
            ? 'Existing shared source linked to your reader too. No duplicate source was created.'
            : 'Source added to the shared list and kept PAUSED. No monitoring has started.',
      });
      await loadSources(accountId);
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Telegram source could not be selected.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  async function removeSource(source: TelegramSelectableSource) {
    if (!accountId || !source.source_id) return;
    setBusyAction(`remove:${source.source_id}`);
    setNotice(null);
    try {
      const response = await fetch(
        `${apiBaseUrl}/admin/telegram/sources/accounts/${accountId}/selected/${source.source_id}/remove`,
        {
          method: 'POST',
          credentials: 'include',
          headers: { Accept: 'application/json' },
        },
      );
      await readJson<{ removed: boolean }>(response);
      setNotice({
        tone: 'success',
        message: sharedCatalogueAvailable
          ? 'Your reader was removed. The shared source remains if another private reader still supplies access.'
          : 'Source selection removed from this Telegram reader.',
      });
      await loadSources(accountId);
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error instanceof Error ? error.message : 'Source selection could not be removed.',
      });
    } finally {
      setBusyAction(null);
    }
  }

  return (
    <section className="telegram-panel" aria-labelledby="telegram-source-selector-title">
      <div className="telegram-panel__header">
        <div>
          <span className="status-label">Shared signal sources</span>
          <h2 id="telegram-source-selector-title">Telegram groups &amp; channels</h2>
          <p>
            Use Shadow for unknown providers: signals are simulated separately and never affect the main paper portfolio. Every state change is audited.
          </p>
        </div>
        {!expanded && (
          <button className="button button--quiet" type="button" onClick={() => void handleExpand()}>
            Manage signal sources
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

          <div className="foundation-note">
            <span className="pulse" aria-hidden="true" />
            Shadow sources are read and simulated in an isolated ledger. They never open MT5 positions,
            notify members or alter the app's daily and weekly figures. Telegram sessions remain private.
          </div>

          {ownerAlerts !== null && (
            <section className="owner-alerts" aria-labelledby="owner-alerts-title">
              <div className="owner-alerts__header">
                <span className="status-label">Owner only</span>
                <h3 id="owner-alerts-title">Source-state alerts</h3>
              </div>
              {ownerAlerts.length === 0 ? (
                <p className="muted-copy">No Trading Admin source-state changes have been recorded yet.</p>
              ) : (
                <div className="owner-alert-list">
                  {ownerAlerts.map((alert) => (
                    <article className="owner-alert" key={alert.event_id}>
                      <strong>
                        {alert.actor_display_name} changed {alert.title} to {alert.status.toUpperCase()}
                      </strong>
                      <small>
                        {alert.previous_status.toUpperCase()} → {alert.status.toUpperCase()} · {formatAlertDate(alert.changed_at)}
                      </small>
                    </article>
                  ))}
                </div>
              )}
            </section>
          )}

          {busyAction === 'accounts' && <p className="muted-copy">Loading Telegram readers…</p>}

          <div>
            <span className="status-label">Shared across Super Signals</span>
            <h3>Added signal sources</h3>
            {!sharedCatalogueAvailable ? (
              <p className="muted-copy">The shared source catalogue is not available.</p>
            ) : sharedSources.length === 0 ? (
              <p className="muted-copy">No shared Telegram signal sources have been added yet.</p>
            ) : (
              <div className="telegram-account-list" aria-label="Shared Super Signals sources">
                {sharedSources.map((source) => (
                  <article className="telegram-account source-state-card" key={source.source_id}>
                    <div className="source-state-card__copy">
                      <span className={`connection-status connection-status--${source.status}`}>
                        SHARED · {source.status.toUpperCase()}
                      </span>
                      <h3>{source.title}</h3>
                      <small>Visible to all authorised Super Signals admins</small>
                      {source.status === 'shadow' && (
                        <small>
                          Shadow: {source.shadow_closed} closed · {source.shadow_wins}W/{source.shadow_losses}L ·{' '}
                          {Number(source.shadow_return_percent) >= 0 ? '+' : ''}
                          {Number(source.shadow_return_percent).toFixed(2)}%
                          {source.shadow_open > 0 ? ` · ${source.shadow_open} open` : ''}
                        </small>
                      )}
                    </div>
                    <div className="source-state-controls" aria-label={`Change ${source.title} state`}>
                      {(['shadow', 'testing', 'live', 'paused'] as SourceState[]).map((state) => (
                        <button
                          className={`source-state-button source-state-button--${state}`}
                          type="button"
                          key={state}
                          aria-pressed={source.status === state}
                          disabled={busyAction !== null || source.status === state}
                          onClick={() => void changeSourceStatus(source, state)}
                        >
                          {busyAction === `status:${source.source_id}:${state}`
                            ? 'Saving…'
                            : state[0].toUpperCase() + state.slice(1)}
                        </button>
                      ))}
                    </div>
                  </article>
                ))}
              </div>
            )}
          </div>

          {accounts.length > 0 && (
            <>
              <div>
                <span className="status-label">Your private reader</span>
                <h3>Add a group or channel</h3>
              </div>
              {accounts.length > 1 && (
                <label>
                  Telegram reader
                  <select
                    value={accountId}
                    onChange={(event) => {
                      setAccountId(event.currentTarget.value);
                      setSources([]);
                      setLoaded(false);
                      setNotice(null);
                    }}
                  >
                    {accounts.map((account) => (
                      <option key={account.id} value={account.id}>
                        {account.label} · {account.phone_hint}
                      </option>
                    ))}
                  </select>
                </label>
              )}

              <button
                className="button"
                type="button"
                onClick={() => void loadSources()}
                disabled={busyAction !== null || !accountId}
              >
                {busyAction === 'discover'
                  ? 'Loading groups & channels…'
                  : loaded
                    ? 'Refresh groups & channels'
                    : 'Show groups & channels'}
              </button>
            </>
          )}

          {loaded && sources.length === 0 && (
            <p className="muted-copy">No selectable Telegram groups or channels were found.</p>
          )}

          {sources.length > 0 && (
            <div className="telegram-account-list" aria-label="Selectable Telegram groups and channels">
              {sources.map((source) => {
                const managedByThisReader = source.managed_by_this_reader !== false;
                return (
                  <article className="telegram-account" key={`${source.kind}:${source.chat_id}`}>
                    <div>
                      <span
                        className={`connection-status ${
                          source.selected ? `connection-status--${source.status ?? 'paused'}` : ''
                        }`}
                      >
                        {source.selected
                          ? managedByThisReader
                            ? `SELECTED · ${(source.status ?? 'paused').toUpperCase()}`
                            : `ALREADY SHARED · ${(source.status ?? 'paused').toUpperCase()}`
                          : source.kind.toUpperCase()}
                      </span>
                      <h3>{source.title}</h3>
                      <small>{source.kind === 'group' ? 'Telegram group' : 'Telegram channel'}</small>
                    </div>
                    <div className="telegram-account__actions">
                      {source.selected ? (
                        managedByThisReader ? (
                          <button
                            className="button button--quiet"
                            type="button"
                            onClick={() => void removeSource(source)}
                            disabled={busyAction !== null}
                          >
                            {busyAction === `remove:${source.source_id}`
                              ? 'Removing…'
                              : 'Remove my reader'}
                          </button>
                        ) : (
                          <button
                            className="button button--quiet"
                            type="button"
                            onClick={() => void selectSource(source)}
                            disabled={busyAction !== null}
                          >
                            {busyAction === `select:${source.chat_id}`
                              ? 'Linking…'
                              : 'Add my reader too'}
                          </button>
                        )
                      ) : (
                        <button
                          className="button"
                          type="button"
                          onClick={() => void selectSource(source)}
                          disabled={busyAction !== null}
                        >
                          {busyAction === `select:${source.chat_id}` ? 'Selecting…' : 'Add to shared sources'}
                        </button>
                      )}
                    </div>
                  </article>
                );
              })}
            </div>
          )}
        </div>
      )}
    </section>
  );
}
