import { afterEach, describe, expect, it, vi } from 'vitest';

import { installApiFetchGuard } from '../src/apiFetchGuard';

describe('apiFetchGuard session restoration', () => {
  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
  });

  it('aborts a hanging auth/me read after four seconds instead of hanging forever', async () => {
    vi.useFakeTimers();

    const nativeFetch = vi.fn((_input: RequestInfo | URL, init?: RequestInit) =>
      new Promise<Response>((_resolve, reject) => {
        const signal = init?.signal;
        if (signal?.aborted) {
          reject(new DOMException('Aborted', 'AbortError'));
          return;
        }
        signal?.addEventListener(
          'abort',
          () => reject(new DOMException('Aborted', 'AbortError')),
          { once: true },
        );
      }),
    );
    vi.stubGlobal('fetch', nativeFetch);

    installApiFetchGuard();

    const pending = window.fetch('/api/auth/me', {
      credentials: 'include',
      headers: { Accept: 'application/json' },
    });
    const rejected = expect(pending).rejects.toMatchObject({ name: 'AbortError' });

    await vi.advanceTimersByTimeAsync(3_999);
    expect(nativeFetch).toHaveBeenCalledTimes(1);

    await vi.advanceTimersByTimeAsync(1);
    await rejected;
    expect(nativeFetch).toHaveBeenCalledTimes(1);
  });
});
