const HOME_HTML = `<!doctype html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <meta name="theme-color" content="#060D14" />
    <meta name="color-scheme" content="dark" />
    <meta name="description" content="Smart Signals tests gold signal providers over time, filters out weak sources and brings approved signals into one trading app." />
    <title>Smart Signals</title>

    <style>html,body{background:#060D14}</style>

    <link rel="preconnect" href="https://fonts.googleapis.com" />
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
    <link rel="preconnect" href="https://api.fontshare.com" />
    <link rel="preconnect" href="https://cdn.fontshare.com" crossorigin />
    <link href="https://fonts.googleapis.com/css2?family=Archivo:wght@600;700;800&family=JetBrains+Mono:wght@400;500&display=swap" rel="stylesheet" />
    <link href="https://api.fontshare.com/v2/css?f[]=satoshi@400,500,600&display=swap" rel="stylesheet" />

    <link rel="icon" href="/assets/logo/smart-signals-mark.svg" type="image/svg+xml" />
    <link rel="apple-touch-icon" href="/assets/logo/smart-signals-mark.svg" />
    <link rel="stylesheet" href="/tokens.css" />
    <link rel="stylesheet" href="/base.css" />
    <link rel="stylesheet" href="/site-sections.css" />
    <link rel="stylesheet" href="/homepage-auth.css?v=20260830-3" />
    <script src="/app.js" defer></script>
  </head>
  <body>
    <header class="site-header" aria-label="Primary">
      <div class="nav-shell glass">
        <a class="brand" href="#top" aria-label="Smart Signals home">
          <picture>
            <source srcset="/assets/logo/smart-signals-approved.webp" type="image/webp" />
            <img class="brand-logo" src="/assets/logo/smart-signals-logo.svg" alt="Smart Signals" />
          </picture>
        </a>

        <nav class="desktop-nav" aria-label="Main navigation">
          <a href="#results">Results</a>
          <a href="#how-it-works">How it works</a>
          <a href="#smart-signals-trading">Smart Signals Trading</a>
          <a href="#faq">FAQ</a>
        </nav>

        <div class="home-auth-actions" aria-label="Smart Signals account access">
          <a class="home-auth-login" href="/join.html?mode=login#account">Log in</a>
          <a class="home-auth-signup" href="/join.html?mode=signup#account">Sign up</a>
          <a class="home-auth-account" href="/account.html" hidden>My account</a>
        </div>

        <div class="mobile-header-auth" aria-label="Smart Signals account access">
          <a class="mobile-header-login" href="/join.html?mode=login#account">Log in</a>
          <a class="mobile-header-signup" href="/join.html?mode=signup#account">Sign up</a>
          <a class="mobile-header-account" href="/account.html" hidden>My account</a>
        </div>

        <button class="menu-toggle" type="button" aria-label="Open navigation" aria-expanded="false" aria-controls="mobile-menu" data-menu-toggle>
          <span></span><span></span><span></span>
        </button>

        <nav class="mobile-menu glass" id="mobile-menu" aria-label="Mobile navigation" data-mobile-menu hidden>
          <a href="#results">Results</a>
          <a href="#how-it-works">How it works</a>
          <a href="#smart-signals-trading">Smart Signals Trading</a>
          <a href="#faq">FAQ</a>
          <a class="mobile-auth-login" href="/join.html?mode=login#account">Log in</a>
          <a class="mobile-auth-signup" href="/join.html?mode=signup#account">Sign up</a>
          <a class="mobile-auth-account" href="/account.html" hidden>My account</a>
        </nav>
      </div>
    </header>

    <main id="top">
      <section class="hero" aria-labelledby="hero-title">
        <picture class="hero-backdrop" aria-hidden="true">
          <source srcset="/assets/hero/smart-signals-hero-gate.webp" type="image/webp" />
          <img src="/assets/hero/smart-signals-hero-gate.jpg" width="2560" height="1440" alt="" fetchpriority="high" decoding="async" />
        </picture>
        <div class="hero-shade" aria-hidden="true"></div>

        <div class="hero-inner">
          <div class="hero-copy">
            <p class="hero-eyebrow">Real gold signals. Properly tested.</p>
            <h1 id="hero-title">Trade gold with signals that have already proved themselves.</h1>
            <p class="hero-sub">We subscribe to signal channels, record what they actually do and test them over time. The sources that earn approval are brought together inside Smart Signals Trading so you do not have to spend months finding them yourself.</p>
            <div class="hero-actions" aria-label="Hero actions">
              <a class="hero-button hero-button--primary" href="#results">See results</a>
              <a class="hero-button hero-button--secondary" href="/how-smart-signals-works.html">See how it works</a>
            </div>
          </div>

          <div class="gate-stage" aria-label="Signal sources approach the Smart Signals gate and only a small number pass through.">
            <canvas class="gate-canvas" data-gate-canvas aria-hidden="true"></canvas>
            <div class="gate-line" aria-hidden="true"></div>
          </div>
        </div>
      </section>

      <section class="content-section" id="what-we-do" aria-labelledby="what-we-do-title">
        <div class="section-shell">
          <div class="section-head">
            <p class="section-kicker">What Smart Signals actually does</p>
            <h2 id="what-we-do-title">We do the expensive, slow part before a signal reaches you.</h2>
            <p class="section-lead">A polished Telegram channel can look convincing in a day. Real performance takes longer to judge. Smart Signals is built to separate the two.</p>
          </div>

          <div class="value-grid">
            <article class="value-card">
              <span class="value-card__num">01</span>
              <h3>We pay for access</h3>
              <p>We join signal channels as normal members and see the same entries, updates and closes that customers see.</p>
            </article>
            <article class="value-card">
              <span class="value-card__num">02</span>
              <h3>We track the real record</h3>
              <p>Calls are recorded over time so a good screenshot or a lucky week cannot stand in for sustained performance.</p>
            </article>
            <article class="value-card">
              <span class="value-card__num">03</span>
              <h3>Only approved sources reach the app</h3>
              <p>The point is not to collect more signal groups. It is to reduce them to the ones that have earned a place.</p>
            </article>
          </div>
        </div>
      </section>

      <section class="content-section smart-trading" id="smart-signals-trading" aria-labelledby="trading-title">
        <div class="section-shell split-grid">
          <div class="split-copy">
            <p class="section-kicker">Smart Signals Trading</p>
            <h2 id="trading-title">One place for the signals that make the cut.</h2>
            <p>Smart Signals Trading is the app that receives approved gold signal instructions and keeps the trade flow organised. Entry, stop, targets, updates, cancellations and closes are treated as parts of the same instruction, not as isolated messages.</p>
            <p>That matters because the job is not simply finding a good entry. The system has to keep understanding the provider after the trade is open.</p>
            <a class="copy-link" href="/how-smart-signals-works.html">See exactly how Smart Signals works</a>
          </div>

          <div class="trading-stack" aria-label="Smart Signals Trading workflow">
            <article class="trading-card">
              <span class="trading-pill">Approved source</span>
              <h3>Signal received</h3>
              <p>The original provider message enters the Smart Signals workflow.</p>
            </article>
            <article class="trading-card">
              <span class="trading-pill">Understood in context</span>
              <h3>Instruction structured</h3>
              <p>The message is interpreted using the rules and language of that source.</p>
            </article>
            <article class="trading-card">
              <span class="trading-pill">Managed in the app</span>
              <h3>Trade state followed</h3>
              <p>Later edits, stop changes, targets, cancellations and closes stay connected to the original trade.</p>
            </article>
          </div>
        </div>
      </section>

      <section class="content-section results-section" id="results" aria-labelledby="results-title">
        <div class="section-shell">
          <div class="section-head">
            <p class="section-kicker">Results you can actually review</p>
            <h2 id="results-title">Performance should be visible by day, not hidden behind a screenshot.</h2>
            <p class="section-lead">The public results area is structured around the same reference account and recorded trading history used inside the app. Figures are only published after reconciliation. We do not fill gaps with invented marketing numbers.</p>
          </div>

          <div class="results-grid">
            <article class="result-card">
              <h3>Reference account</h3>
              <p>The public performance series starts from the agreed reference account used for Smart Signals reporting.</p>
              <div class="metric-stack">
                <div class="metric-row">
                  <span class="metric-label">Starting balance</span>
                  <strong class="metric-value metric-value--green">$1,000</strong>
                </div>
                <div class="metric-row">
                  <span class="metric-label">Tracking start</span>
                  <strong class="metric-value">8 Aug 2026</strong>
                </div>
                <div class="metric-row">
                  <span class="metric-label">Daily P&amp;L</span>
                  <strong class="metric-value">Published after reconciliation</strong>
                </div>
                <div class="metric-row">
                  <span class="metric-label">Method</span>
                  <strong class="metric-value">Recorded app results</strong>
                </div>
              </div>
              <p class="result-note">Monthly profit, total trades, win rate, account growth, best day and worst day will be populated from reconciled results rather than guessed values.</p>
            </article>

            <article class="result-card" aria-labelledby="calendar-title">
              <h3 id="calendar-title">August 2026 profit calendar</h3>
              <p>Each day is designed to show the reconciled result for that date. The calendar is already part of the site structure, without fake daily figures.</p>
              <div class="calendar-wrap" aria-label="August 2026 profit calendar structure">
                <div class="calendar-head" aria-hidden="true">
                  <span>Mon</span><span>Tue</span><span>Wed</span><span>Thu</span><span>Fri</span><span>Sat</span><span>Sun</span>
                </div>
                <div class="calendar-grid">
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day"><span>1</span></div>
                  <div class="calendar-day"><span>2</span></div>
                  <div class="calendar-day"><span>3</span></div>
                  <div class="calendar-day"><span>4</span></div>
                  <div class="calendar-day"><span>5</span></div>
                  <div class="calendar-day"><span>6</span></div>
                  <div class="calendar-day"><span>7</span></div>
                  <div class="calendar-day calendar-day--reference"><span>8</span><span class="calendar-state">Start</span></div>
                  <div class="calendar-day"><span>9</span></div>
                  <div class="calendar-day"><span>10</span></div>
                  <div class="calendar-day"><span>11</span></div>
                  <div class="calendar-day"><span>12</span></div>
                  <div class="calendar-day"><span>13</span></div>
                  <div class="calendar-day"><span>14</span></div>
                  <div class="calendar-day"><span>15</span></div>
                  <div class="calendar-day"><span>16</span></div>
                  <div class="calendar-day"><span>17</span></div>
                  <div class="calendar-day"><span>18</span></div>
                  <div class="calendar-day"><span>19</span></div>
                  <div class="calendar-day"><span>20</span></div>
                  <div class="calendar-day"><span>21</span></div>
                  <div class="calendar-day"><span>22</span></div>
                  <div class="calendar-day"><span>23</span></div>
                  <div class="calendar-day"><span>24</span></div>
                  <div class="calendar-day"><span>25</span></div>
                  <div class="calendar-day"><span>26</span></div>
                  <div class="calendar-day"><span>27</span></div>
                  <div class="calendar-day"><span>28</span></div>
                  <div class="calendar-day calendar-day--reference"><span>29</span><span class="calendar-state">Current</span></div>
                  <div class="calendar-day"><span>30</span></div>
                  <div class="calendar-day"><span>31</span></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                  <div class="calendar-day calendar-day--empty"></div>
                </div>
              </div>
            </article>
          </div>
        </div>
      </section>

      <section class="content-section how-section" id="how-it-works" aria-labelledby="how-title">
        <div class="section-shell">
          <div class="section-head">
            <p class="section-kicker">How it works</p>
            <h2 id="how-title">Find. Test. Understand. Follow.</h2>
            <p class="section-lead">The homepage gives you the short version. The full process is explained on a dedicated page.</p>
          </div>

          <div class="home-steps">
            <article class="step-card">
              <span class="step-card__num">01</span>
              <h3>Find sources worth testing</h3>
              <p>We subscribe to channels and watch what they actually send to members.</p>
            </article>
            <article class="step-card">
              <span class="step-card__num">02</span>
              <h3>Test the full record</h3>
              <p>Entries, updates, wins and losses are recorded over time.</p>
            </article>
            <article class="step-card">
              <span class="step-card__num">03</span>
              <h3>Learn how each source writes</h3>
              <p>Approved sources are handled according to their own message style and trade language.</p>
            </article>
            <article class="step-card">
              <span class="step-card__num">04</span>
              <h3>Follow the trade through</h3>
              <p>Smart Signals Trading keeps later changes connected to the trade after entry.</p>
            </article>
          </div>
          <a class="copy-link" href="/how-smart-signals-works.html">Read the full Smart Signals process</a>
        </div>
      </section>

      <section class="content-section" id="trust" aria-labelledby="trust-title">
        <div class="section-shell">
          <div class="section-head">
            <p class="section-kicker">Built for the awkward parts</p>
            <h2 id="trust-title">A signal service has to work when the message changes.</h2>
            <p class="section-lead">The real test is not a clean winning trade. It is what happens when the provider edits the plan, cancels an order, changes a stop or sends the same idea twice.</p>
          </div>
          <div class="trust-grid">
            <article class="trust-card">
              <span class="trust-card__eyebrow">Stops</span>
              <h3>Protection is part of the instruction</h3>
              <p>Stops are treated as part of the trade plan rather than something to remember later.</p>
            </article>
            <article class="trust-card">
              <span class="trust-card__eyebrow">Updates</span>
              <h3>The trade stays connected</h3>
              <p>Later messages are interpreted against the open trade so changes are not treated as brand new calls.</p>
            </article>
            <article class="trust-card">
              <span class="trust-card__eyebrow">Review</span>
              <h3>Approval has to keep being earned</h3>
              <p>Sources stay under review. A provider does not keep its place simply because it was once good enough to get in.</p>
            </article>
          </div>
        </div>
      </section>

      <section class="content-section" id="faq" aria-labelledby="faq-title">
        <div class="section-shell">
          <div class="section-head">
            <p class="section-kicker">FAQ</p>
            <h2 id="faq-title">The obvious questions.</h2>
          </div>
          <div class="faq-list">
            <article class="faq-item">
              <h3>Is Smart Signals another gold signal group?</h3>
              <p>No. Smart Signals is the filter and trading layer in front of signal sources. The service is built around testing sources before they are approved.</p>
            </article>
            <article class="faq-item">
              <h3>Do I need to join every signal channel myself?</h3>
              <p>The point of Smart Signals is to avoid making you spend months buying and comparing channels yourself. Approved sources are brought together through the service.</p>
            </article>
            <article class="faq-item">
              <h3>Are profits guaranteed?</h3>
              <p>No. Gold trading carries risk and past results do not guarantee future performance. The value of the service is in the filtering, tracking and trade workflow, not a promise that every trade wins.</p>
            </article>
            <article class="faq-item">
              <h3>Why publish a profit calendar?</h3>
              <p>Because a monthly headline can hide a lot. Daily results make the shape of performance easier to review, including difficult days as well as profitable ones.</p>
            </article>
          </div>
        </div>
      </section>

      <div class="final-cta-wrap" id="start">
        <section class="final-cta" aria-labelledby="start-title">
          <p class="section-kicker">Smart Signals</p>
          <h2 id="start-title">Real gold signals. Properly tested.</h2>
          <p>Get the approved signal sources, the filtering work behind them and Smart Signals Trading in one service.</p>
          <div class="final-cta__actions">
            <a class="hero-button hero-button--primary" href="#start">Start at €99/month</a>
            <a class="hero-button hero-button--secondary" href="/how-smart-signals-works.html">Read how it works</a>
          </div>
        </section>
      </div>
    </main>

    <footer class="site-footer">
      <div class="footer-inner">
        <a class="footer-brand" href="#top" aria-label="Smart Signals home">
          <picture>
            <source srcset="/assets/logo/smart-signals-approved.webp" type="image/webp" />
            <img src="/assets/logo/smart-signals-logo.svg" alt="Smart Signals" />
          </picture>
        </a>
        <nav class="footer-links" aria-label="Footer navigation">
          <a href="#results">Results</a>
          <a href="#how-it-works">How it works</a>
          <a href="#faq">FAQ</a>
          <a href="/how-smart-signals-works.html">Full process</a>
        </nav>
        <p class="footer-note">Smart Signals · structured trade intelligence</p>
      </div>
    </footer>

    <script>
      (async () => {
        try {
          const response = await fetch('/account-api/auth/me', {
            credentials: 'include',
            cache: 'no-store',
            headers: { Accept: 'application/json' }
          });
          const signedIn = response.ok;
          document.querySelectorAll('.home-auth-login,.home-auth-signup,.mobile-header-login,.mobile-header-signup,.mobile-auth-login,.mobile-auth-signup').forEach((item) => {
            item.hidden = signedIn;
          });
          document.querySelectorAll('.home-auth-account,.mobile-header-account,.mobile-auth-account').forEach((item) => {
            item.hidden = !signedIn;
          });
        } catch {
          /* Logged-out controls remain visible if account status cannot be read. */
        }
      })();
    </script>
  </body>
</html>`;
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

function htmlResponse(body, marker) {
  return new Response(body, {
    status: 200,
    headers: {
      'content-type': 'text/html; charset=utf-8',
      'cache-control': 'no-store, no-cache, must-revalidate, max-age=0',
      'pragma': 'no-cache',
      'x-smart-signals-recovery': marker
    }
  });
}

export default {
  async fetch(request) {
    const url = new URL(request.url);

    if (url.hostname === 'www.smartsignals.site') {
      url.hostname = 'smartsignals.site';
      return Response.redirect(url.toString(), 301);
    }

    if (url.pathname === '/index.html') {
      url.pathname = '/';
      return Response.redirect(url.toString(), 301);
    }

    if (url.pathname === '/' || url.pathname === '') {
      return htmlResponse(HOME_HTML, 'homepage-2026-09-24');
    }

    if (url.pathname === '/performance.html') {
      url.pathname = '/performance';
      return Response.redirect(url.toString(), 301);
    }

    if (url.pathname === '/performance' || url.pathname === '/performance/') {
      return htmlResponse(PERFORMANCE_HTML, 'performance-2026-09-24');
    }

    const upstream = new URL(request.url);
    upstream.protocol = 'https:';
    upstream.hostname = 'super-signals-website.dannythehat2.workers.dev';
    upstream.port = '';

    const headers = new Headers(request.headers);
    headers.set('host', upstream.hostname);

    const proxied = new Request(upstream.toString(), {
      method: request.method,
      headers,
      body: request.method === 'GET' || request.method === 'HEAD' ? undefined : request.body,
      redirect: 'manual'
    });

    return fetch(proxied);
  }
};
