import { useState } from 'react';

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

interface SharedTelegramSource {
  source_id: string;
  chat_id: number;
  title: string;
  status: string;
}

interface TelegramSourceSelectorProps {
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

export function TelegramSourceSelector({ apiBaseUrl }: TelegramSourceSelectorProps) {
  const [expanded, setExpanded] = useState(false);
  const [accounts, setAccounts] = useState<TelegramAccount[]>([]);
  const [accountId, setAccountId] = useState('');
  const [sources, setSources] = useState<TelegramSelectableSource[]>([]);
  const [sharedSources, setSharedSources] = useState<SharedTelegramSource[]>([]);
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
          message: 'Connect your own Telegram reader above before adding another signal source.',
        });
      }
      await fetchSharedSources();
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
      // Defence in depth: never render a user/private-chat row even if a server regression occurs.
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
          ? 'Source selected and kept PAUSED. This confirms the real Telegram group-selection path; the shared catalogue will appear when the Day 10 backend is deployed.'
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
          ? 'Your reader was removed from this source. The shared source stays available if another reader still supplies access.'
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
            You and the other authorised admins share the groups and channels deliberately added to
            Super Signals. Your Telegram login and session stay private to you.
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
            Shared source names and trade activity are visible to authorised admins, but Telegram
            sessions remain private. Sources stay PAUSED until a later build step explicitly enables
            listening.
          </div>

          {busyAction === 'accounts' && <p className="muted-copy">Loading Telegram readers…</p>}

          <div>
            <span className="status-label">Shared across Super Signals</span>
            <h3>Added signal sources</h3>
            {!sharedCatalogueAvailable ? (
              <p className="muted-copy">
                The shared Day 10 catalogue is not live on the backend yet. You can still use this
                screen to smoke-test adding one real Telegram group as PAUSED.
              </p>
            ) : sharedSources.length === 0 ? (
              <p className="muted-copy">No shared Telegram signal sources have been added yet.</p>
            ) : (
              <div className="telegram-account-list" aria-label="Shared Super Signals sources">
                {sharedSources.map((source) => (
                  <article className="telegram-account" key={source.source_id}>
                    <div>
                      <span className="connection-status connection-status--connected">
                        SHARED · {source.status.toUpperCase()}
                      </span>
                      <h3>{source.title}</h3>
                      <small>Visible to all authorised Super Signals admins</small>
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
                          source.selected ? 'connection-status--connected' : ''
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
