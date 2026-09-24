const PERFORMANCE_HTML = `<!doctype html>
<html lang="en">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <meta name="theme-color" content="#061018" />
  <meta name="color-scheme" content="dark" />
  <meta name="description" content="Smart Signals public Gold trading performance: daily profit and loss, balances, calendar history and live tracking." />
  <title>Performance | Smart Signals</title>
  <style>html,body{background:#061018}</style>
  <link rel="icon" href="/assets/logo/smart-signals-mark.svg" type="image/svg+xml" />
  <link rel="stylesheet" href="/performance-launch.css?v=20260830-mobile2" />
  <link rel="stylesheet" href="/performance-mobile-ledger.css?v=20260830-mobile1" />
  <link rel="stylesheet" href="/performance-live.css?v=20260831-live1" />
  <link rel="stylesheet" href="/performance-balance-fit.css?v=20260903-balance2" />
  <link rel="stylesheet" href="/performance-trade-detail.css?v=20260903-trades1" />
  <script src="/performance.js?v=20260924-canonical3" defer></script>
</head>
<body>
  <header class="perf-header">
    <a href="/" aria-label="Smart Signals home"><img src="/assets/logo/smart-signals-approved.webp" alt="Smart Signals" /></a>
    <a href="/account">My account</a>
  </header>

  <main class="perf-shell">
    <section class="panel hero">
      <p class="eyebrow">Smart Signals performance</p>
      <h1>Daily profit. Daily loss. One running balance.</h1>
      <p>Every win and every loss is tracked. Nothing is hidden, removed or cherry-picked. Results are recorded day by day from 6 August 2026.</p>
      <div class="hero-meta">
        <span class="chip">XAUUSD / Gold</span>
        <span class="chip">Daily P/L</span>
        <span class="chip chip--green">Live from $1,517.23</span>
      </div>
    </section>

    <section class="summary-grid" aria-label="Performance summary">
      <article class="stat stat--start"><span>Live starting balance</span><strong data-start-balance>$1,517.23</strong><small>31 Aug 2026</small></article>
      <article class="stat stat--end"><span>Current recorded balance</span><strong data-end-balance>$2,075.81</strong><small data-current-balance-note>Verified through 22 Sep 2026</small></article>
      <article class="stat stat--pnl"><span>Total recorded P/L</span><strong class="positive" data-net-pnl>+$773.43</strong><small data-return-pct>+77.34%</small></article>
      <article class="stat stat--period"><span>Recorded trading days</span><strong data-trading-days>33</strong><small data-win-loss-days>22 profit · 11 loss</small></article>
    </section>

    <section class="panel section" id="history">
      <div class="section-head">
        <div><p class="eyebrow">Daily transparency</p><h2>Trading calendar</h2><p>Tap a trading day to see its start balance, P/L, end balance and the trades recorded that day.</p></div>
        <div class="month-switcher"><button type="button" data-month-prev aria-label="Previous month">←</button><strong data-month-label>September 2026</strong><button type="button" data-month-next aria-label="Next month">→</button></div>
      </div>

      <div class="history-grid">
        <div class="calendar-column">
          <div class="calendar-weekdays" aria-hidden="true"><span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span><span>Sun</span></div>
          <div class="calendar" data-calendar></div>
        </div>
        <aside class="day-card" data-day-detail>
          <span class="label">Verified live day</span>
          <h3>21 September 2026</h3>
          <div class="day-stat"><span>Start</span><strong>$1,789.44</strong></div>
          <div class="day-stat"><span>Profit / loss</span><strong class="negative">-$16.01</strong></div>
          <div class="day-stat"><span>End</span><strong>$1,773.43</strong></div>
        </aside>
      </div>
    </section>

    <section class="panel section">
      <div class="section-head"><div><p class="eyebrow">Full daily ledger</p><h2>Balance by trading day</h2><p>Historical bridge plus verified live days.</p></div></div>
      <div class="table-wrap"><table class="ledger-table"><thead><tr><th>Date</th><th>Start balance</th><th>Profit / loss</th><th>End balance</th><th>Record</th></tr></thead><tbody data-ledger-body></tbody></table></div>
      <p class="ledger-footnote">6–28 August figures are the reconstructed historical bridge. From 31 August onward, the public ledger updates automatically from the same canonical trading records used by Smart Signals. Individual trade drill-down is recorded from 3 September onward.</p>
    </section>

    <section class="panel section member-trades" data-member-trades hidden>
      <div class="member-trade-head">
        <div><p class="eyebrow">Your Smart Signals activity</p><h2>Open and completed trades</h2><p>The same trade history shown in the Smart Signals app.</p></div>
        <span class="member-sync" data-member-sync><strong>LIVE</strong> · account synced</span>
      </div>
      <div class="member-trade-list" data-member-trade-list></div>
    </section>

    <section class="challenge">
      <article class="panel challenge-card"><p class="eyebrow">Separate funded challenge</p><h2>$100 → $1,000 Challenge</h2><p>This remains separate from the main Smart Signals performance ledger. It begins on its own funded account on 10 September 2026, with every daily result recorded independently.</p></article>
      <aside class="challenge-balance"><span>Challenge status</span><strong>Starts at $100</strong><p>10 Sep 2026</p></aside>
    </section>
  </main>

  <footer class="perf-footer">Historical performance does not guarantee future results. <a href="/#onboarding">Join Smart Signals</a></footer>
</body>
</html>`;
const ORIGIN = 'https://super-signals-website.dannythehat2.workers.dev';

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.pathname === '/performance.html') {
      url.pathname = '/performance';
      return Response.redirect(url.toString(), 301);
    }

    if (url.pathname === '/performance' || url.pathname === '/performance/') {
      return new Response(PERFORMANCE_HTML, {
        status: 200,
        headers: {
          'content-type': 'text/html; charset=utf-8',
          'cache-control': 'no-store, no-cache, must-revalidate, max-age=0',
          'pragma': 'no-cache',
          'x-smart-signals-performance-hotfix': '2026-09-24'
        }
      });
    }

    const upstream = new URL(request.url);
    upstream.protocol = 'https:';
    upstream.hostname = 'super-signals-website.dannythehat2.workers.dev';

    const proxied = new Request(upstream.toString(), request);
    return fetch(proxied, { redirect: 'manual' });
  }
};
