const TRANSIENT_STATUS = new Set([502, 503, 504]);
const RETRY_DELAYS_MS = [350, 900];
const SESSION_READ_TIMEOUT_MS = 4_000;

let installed = false;

function sleep(milliseconds: number): Promise<void> {
  return new Promise((resolve) => window.setTimeout(resolve, milliseconds));
}

function requestMethod(input: RequestInfo | URL, init?: RequestInit): string {
  if (init?.method) return init.method.toUpperCase();
  if (input instanceof Request) return input.method.toUpperCase();
  return 'GET';
}

function requestPath(input: RequestInfo | URL): string {
  const raw = input instanceof Request ? input.url : String(input);
  try {
    return new URL(raw, window.location.href).pathname;
  } catch {
    return raw;
  }
}

function acceptsJson(input: RequestInfo | URL, init?: RequestInit): boolean {
  const headers = new Headers(input instanceof Request ? input.headers : undefined);
  if (init?.headers) {
    new Headers(init.headers).forEach((value, key) => headers.set(key, value));
  }
  return (headers.get('accept') || '').toLowerCase().includes('application/json');
}

function isHtml(response: Response): boolean {
  return (response.headers.get('content-type') || '').toLowerCase().includes('text/html');
}

function isSessionRestoreRead(input: RequestInfo | URL, method: string): boolean {
  return (method === 'GET' || method === 'HEAD') && requestPath(input).endsWith('/auth/me');
}

async function sessionFetchWithTimeout(
  nativeFetch: typeof window.fetch,
  input: RequestInfo | URL,
  init?: RequestInit,
): Promise<Response> {
  const controller = new AbortController();
  const upstreamSignal = init?.signal ?? (input instanceof Request ? input.signal : undefined);
  const abortFromUpstream = () => controller.abort();

  if (upstreamSignal?.aborted) controller.abort();
  else upstreamSignal?.addEventListener('abort', abortFromUpstream, { once: true });

  const timeout = window.setTimeout(() => controller.abort(), SESSION_READ_TIMEOUT_MS);
  try {
    return await nativeFetch(input, { ...init, signal: controller.signal });
  } finally {
    window.clearTimeout(timeout);
    upstreamSignal?.removeEventListener('abort', abortFromUpstream);
  }
}

function temporaryJsonResponse(): Response {
  return new Response(
    JSON.stringify({
      detail: {
        code: 'api_temporarily_unavailable',
        message: 'Live data is reconnecting. Please try again in a moment.',
      },
    }),
    {
      status: 503,
      headers: {
        'Content-Type': 'application/json; charset=utf-8',
        'Cache-Control': 'no-store',
      },
    },
  );
}

/**
 * Render can return its own HTML 502/503 page during a brief instance transition.
 * JSON consumers must never try to parse that HTML as application data.
 *
 * GET/HEAD JSON reads are retried twice. Mutating requests are never retried, so
 * this guard cannot duplicate a trade, close, settings change, or other action.
 * Session restoration is intentionally different: /auth/me gets one bounded four-second
 * attempt and then hands control back to App's existing 1.5-second reconnect loop. This
 * prevents a mobile browser request opened during a Render restart from hanging forever.
 */
export function installApiFetchGuard(): void {
  if (installed) return;
  installed = true;

  const nativeFetch = window.fetch.bind(window);

  window.fetch = async (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    if (!acceptsJson(input, init)) return nativeFetch(input, init);

    const method = requestMethod(input, init);
    const mayRetry = method === 'GET' || method === 'HEAD';
    const sessionRestoreRead = isSessionRestoreRead(input, method);
    const maxAttempts = sessionRestoreRead ? 1 : mayRetry ? RETRY_DELAYS_MS.length + 1 : 1;

    for (let attempt = 0; attempt < maxAttempts; attempt += 1) {
      try {
        const response = sessionRestoreRead
          ? await sessionFetchWithTimeout(nativeFetch, input, init)
          : await nativeFetch(input, init);
        const transient = TRANSIENT_STATUS.has(response.status) || isHtml(response);

        if (transient && mayRetry && attempt < maxAttempts - 1) {
          await sleep(RETRY_DELAYS_MS[attempt]);
          continue;
        }

        if (isHtml(response)) return temporaryJsonResponse();
        return response;
      } catch (error) {
        if (mayRetry && attempt < maxAttempts - 1) {
          await sleep(RETRY_DELAYS_MS[attempt]);
          continue;
        }
        throw error;
      }
    }

    return temporaryJsonResponse();
  };
}
