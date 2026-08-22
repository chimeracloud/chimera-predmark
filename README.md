# Chimera PredMark

Prediction market arbitrage — detection **and** execution — across Polymarket,
Kalshi and Limitless, through a self-hosted [pmxt](https://github.com/pmxt-dev/pmxt)
sidecar.

FastAPI on Cloud Run, Firestore for config and trades, GCS for raw scan data,
Cloud Scheduler for the trigger. The dashboard lives in the CST portal at
**Products › Arbitrage › PredMark** (repo `chimeracloud/cst`), reached through
the portal proxy so it inherits Cloudflare Access and the portal JWT.

---

## Deployment status — 22 August 2026

| Component | State | Detail |
|---|---|---|
| Dashboard | **Live** | CST portal, `/products/arbitrage/predmark/*` (repo `chimeracloud/cst`) |
| Backend | **Live** | `predmark-00003-qvs` on Cloud Run, `europe-west1`, image `predmark:v3` |
| pmxt sidecar | **Running** | `/ready` reports `pmxt_sidecar: true` |
| Portal proxy | **Connected** | `PROXY_PREDMARK` on `cst-api`; its SA holds `run.invoker` here |
| Polymarket | **Authenticated** | balance reads; unfunded ($0.00) |
| Kalshi | **Authenticated** | Kalshi SA account works against the same API; unfunded ($0.00) |
| Limitless | **Disabled** | data unusable — see "Platform coverage" below |
| Artifact Registry | Created | `europe-west1-docker.pkg.dev/chiops/predmark` |
| GCS bucket | Created | `gs://predmark-data` |
| Firestore | In use | collections in the `(default)` database, `europe-west2` |
| Service account | Created | `predmark-sa@chiops.iam.gserviceaccount.com` |
| Cloud Scheduler | **Not enabled** | API off; scans run only when triggered from the dashboard |
| Trading | **Off** | `trading_enabled: false`, `dry_run: true` |
| Tradeable pairs found | **Zero** | not a fault — see below |

### Platform coverage — why nothing trades yet

Measured 22 August 2026 against the live platforms. The engine runs correctly
end to end and finds **no tradeable pairs**. That is a property of the market,
not a fault in the matcher, and it is the open question for the strategy.

**Polymarket 1,374 active markets · Kalshi 1,494 active · not one pair.**

| Subject | Polymarket | Kalshi | Overlap? |
|---|---|---|---|
| Bitcoin | 84 | **0** | none reachable |
| Ethereum | 49 | **0** | none reachable |
| Fed / rates | 31 | 182 | yes, but unmatchable by wording |
| CPI | 0 | 104 | none |
| GDP | 0 | 113 | none |
| NFL | 33 | 27 | different questions |
| NBA | 18 | 27 | different questions |

Three things this shows:

1. **Kalshi returns no Bitcoin or Ethereum price markets through pmxt.** Kalshi
   runs those markets in reality, so they are either paginated beyond the 1,500
   the fetch returns or sit in a series pmxt does not surface. This is the
   largest single unlock available: crypto is where both platforms are deep and
   where questions are most objectively comparable.

2. **The one real overlap is Fed rate decisions, and the wording defeats
   matching.** Polymarket asks *"Will the Fed decrease interest rates by 25 bps
   after the September meeting?"*; Kalshi asks *"Will the upper bound of the
   target range for the federal funds rate in effect at 11:59 PM…"*. Same event,
   almost no shared vocabulary. Title similarity cannot bridge that; a person
   reading both can.

3. **Where both list a sport, they sell different questions.** Polymarket sells
   championship winners, Kalshi sells player retirements and franchise news.
   Kalshi's book is US economics; Polymarket's is global politics and crypto.

**Limitless remains unusable.** It returns the identical order book for both
outcomes of most markets — YES and NO both quoting the same price, which
presented as a 1090% margin before the complementarity check caught it. Of 400
markets fetched, 8 survive. Disabled.

**Routes forward, in order of value:**

1. Reach Kalshi's crypto markets — pagination, series lookup, or a direct
   Kalshi API call outside pmxt's `fetchMarkets`.
2. Hand-map known equivalent series (Fed decisions first). Reliable, manual,
   and it makes the highest-liquidity overlap tradeable.
3. Semantic matching rather than string similarity, which would catch the Fed
   case automatically but is a substantial piece of work.

Until one of these lands, the engine will keep scanning and correctly
reporting nothing.

### What remains

**Infrastructure**
1. Enable `cloudscheduler.googleapis.com` and create the `predmark-scan` job.
2. Add a portal-proxy route so the dashboard can reach Cloud Run. Until then the
   settings page cannot be used and credentials must be written directly to
   Secret Manager.
3. Wire the GitHub Actions deploy (workload identity federation), or keep
   deploying by hand with `gcloud builds submit` and `gcloud run deploy`.

**Before any capital moves**
4. Venue accounts and funded balances. Kalshi went international in October
   2025 and South Africa is not restricted, but whether Kalshi SA quotes the
   same markets as Kalshi US is unproven — the test is an authenticated fetch
   with real credentials.
5. Confirm each venue's fee schedule against the defaults in
   `venues/registry.py`. A wrong fee model does not fail loudly.
6. Exchange control on converting rand to USDC.
7. Identity on the dashboard. It can halt trading, change stake, store wallet
   keys and force an unwind, behind a single WAF IP rule with no audit of who.

### Known production issues found and fixed

- **A destroyed secret version 500s `/scan`.** `FAILED_PRECONDITION` was
  uncaught, so a rotated-away key took detection down for every venue rather
  than reading as "not configured". Fixed in `settings/secrets.py`.
- **Trailing newlines corrupt credentials.** `openssl rand | gcloud
  --data-file=-` and the Secret Manager console both append one; for the
  sidecar token that is an illegal HTTP header value. Now stripped on read and
  on use.
- **Secret Manager IAM is scoped per secret.** The runtime account holds
  `secretVersionAdder` on the six venue secrets rather than project-wide
  `secretmanager.admin`, which in `chiops` would have exposed every other
  service's credentials.

---

## What it does

| Stage | Behaviour |
|---|---|
| **Scan** | Polls enabled venues, pairs equivalent markets, assesses resolution equivalence, prices both legs against real order-book depth net of each venue's fees, records everything |
| **Decide** | Applies the configured filters — margin floor, stake, exposure caps, required resolution status, balances, depth, daily limits, kill switch |
| **Execute** | Submits both legs concurrently, watches fills in seconds, chases a short leg, unwinds what cannot be hedged |
| **Manage** | Tracks fills, contains single-leg fills, holds hedged pairs to resolution, books settlement |
| **Report** | P&L per trade, per venue, per day, with a full audit trail and — the number that matters — the unmatched-leg count |

---

## The bit that decides whether this works

A filled leg with an unfilled counter-leg is not an arbitrage. It is an
unhedged directional position, which is the one outcome this strategy exists
to avoid.

`backend/execution/legs.py` does this, in order:

1. **Submits both legs concurrently** (`asyncio.gather`). Sequential
   submission leaves a window in which the second price moves.
2. **Polls both for fills in seconds**, not at the next scan.
3. **Compares filled sizes.** The hedged quantity is `min(a, b)`; anything
   above that on either side is naked. Unequal partial fills are handled the
   same way as a clean single-leg fill, because they are the same problem —
   and the commoner one.
4. **Chases the short leg** — resubmits for the shortfall at a worse price, up
   to `second_leg_retry_limit` attempts and the `second_leg_reprice_ceiling`.
   Completing a hedge slightly expensively beats unwinding.
5. **Unwinds the excess** if the chase fails. Market sell, retried, no
   haggling: a bad price is cheaper than an unhedged position.
6. **Records a containment failure with its realised cost**, and increments
   the unmatched-leg counters — daily and cumulative — which the dashboard
   shows next to P&L. A profitable week with three unmatched legs means the
   execution layer is broken and got lucky.

If the unwind itself fails, the trade is marked `EXPOSED`, an alert is raised
at critical level, and the settlement notes name the venue, the market and
the share count. That state needs a human and is never reported as anything
else.

An order is **never resubmitted on a timeout**. A request that times out may
have reached the venue, and a blind resend turns one position into two. When
submission is ambiguous the code reconciles against the venue's own positions
(`execution/orders.py: reconcile_ambiguous`).

Tests: `backend/tests/test_execution.py` — both-legs-fill, single-leg fill and
unwind, chase-then-hedge, failed unwind → EXPOSED, partial fills unwinding
only the excess, neither-leg-fills, ambiguous submission reconciliation, and
an assertion that the two legs really are submitted concurrently.

---

## Resolution criteria — the real risk

Two venues can ask what reads as the same question and settle it differently:
closing price versus intraday touch, Binance versus Coinbase, 4pm versus
11:59pm. That looks exactly like arbitrage and it is not — it is two different
bets, and the spread is usually the market correctly pricing the difference.

Every pair carries `resolution_status`, `resolution_a`, `resolution_b` and
`resolution_notes`. `backend/matching/resolution.py` compares five dimensions:

| Dimension | Source |
|---|---|
| `settlement_time` | the venues' published resolution timestamps |
| `basis` | intraday touch / closing price / official result / data release |
| `source` | Binance, Coinbase, Chainlink, BLS, AP, ESPN, … |
| `threshold` | the numeric level and its comparator |
| `cutoff` | stated time of day and timezone |

**Any conflict is `DIFFERS`.** Agreement is `MATCHED` only when settlement
time and basis were positively determined on both sides and no threshold
conflicts. Everything else is `UNVERIFIED`, which is the default — silence is
not agreement.

One decision worth flagging: **basis, source and cutoff are extracted from the
rules text only, never the title.** Two venues asking a question that reads
the same is the premise of the problem, not evidence against it. Taking the
word "close" out of a shared title as proof of a shared settlement basis would
make the module agree with precisely the pairs it exists to catch. Thresholds
are the exception — the title is where venues state them unambiguously.

`DIFFERS` is never tradeable under any setting. `UNVERIFIED` is recorded and
displayed but never traded unless Charles sets **both** `allow_unverified_override`
and `required_resolution_status = MATCHED_OR_UNVERIFIED`; every such trade is
flagged, carries the override reason, and raises a standing dashboard alert.

---

## Credentials

Entered once in the settings UI. Never in Firestore, never in config, never in
the frontend, never in logs, never returned by any endpoint.

```
settings page  --HTTPS-->  PUT /settings/credentials  -->  Secret Manager
                                                              |
                       execution time  <-- read into a local ---
```

- `PUT /settings` **rejects** a payload containing credential-shaped keys
  rather than silently dropping them — a silent drop would leave Charles
  believing a key was saved.
- `GET /settings` returns `configured: true/false` and a masked tail
  (`****cdef`). Values of eight characters or fewer mask entirely, because
  four characters of an eight-character secret is half of it.
- There is no read counterpart to the credential write. Not behind a flag, not
  for debugging.
- Shape validation rejects a truncated paste at write time rather than at
  execution time, mid-trade.
- `backend/logging_setup.py` redacts hex keys, PEM blocks and labelled secrets
  from every log record as a second line of defence. Nothing passes a secret
  to a logger in the first place.

Secrets, created on first write by the settings page:

| Venue | Field | Secret ID |
|---|---|---|
| Polymarket | private key | `predmark-polymarket-private-key` |
| Polymarket | proxy / funder address | `predmark-polymarket-proxy-address` |
| Kalshi | API key ID | `predmark-kalshi-api-key` |
| Kalshi | RSA private key | `predmark-kalshi-private-key` |
| Limitless | API key | `predmark-limitless-api-key` |
| Limitless | signing key | `predmark-limitless-signing-key` |

Plus one secret Charles creates by hand:

| Secret ID | What it is |
|---|---|
| `predmark-pmxt-access-token` | Shared token between uvicorn and the pmxt sidecar *inside* the container. Any random string. Not a venue credential. |

---

## Kill switch

One control on the dashboard. Halts all execution immediately; scanning
continues.

It works without a redeploy and without a restart because the flag lives in
Firestore and **every execution decision reads it fresh** — there is no cached
copy in the process, and it is re-read between individual trades within a
single scan, so "immediately" means immediately. If the read itself fails, the
switch is treated as engaged.

Releasing it does not resume trading on its own: `trading_enabled` is a
separate setting. Two deliberate actions to resume, one to stop.

---

## Architecture

```
Cloud Scheduler (predmark-scan)
        |  POST /scan
        v
Cloud Run: predmark  (europe-west1, chiops, --no-allow-unauthenticated)
  +-----------------------------------------------+
  |  uvicorn :$PORT      pmxt-core :3847          |
  |  FastAPI       <-->  (localhost only)  --> venue APIs
  +-----------------------------------------------+
        |                       |
        v                       v
  Firestore                   GCS
  predmark-config             predmark-data/scans/YYYY/MM/DD/
  predmark-trades
        ^
        |  portal proxy (CHI-POL-004)
  CST portal (Cloudflare Pages: cst)
```

**Why Cloud Scheduler and not a background loop.** The lay engine runs
`minScale=1` with an in-process loop, so the container never recycles and an
expired credential is never refreshed. That has taken the platform down three
times. This service runs `min-instances 0`, is invoked on a schedule, and
reads its credentials from Secret Manager on every cycle. It cannot develop
that failure.

**pmxt is self-hosted, not pmxt.dev.** `pmxt-core` is installed from npm at a
pinned version into the same image and started by the entrypoint on
`127.0.0.1:3847`, unreachable from outside the container. It talks straight to
the venue APIs. The one part of pmxt-core that calls `api.pmxt.dev` is the
`router` pseudo-exchange, and `venues/pmxt_client.py` refuses it outright.

---

## API

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/scan` | One scan cycle. Called by Cloud Scheduler. `?execute=false` to detect only. 409 if a scan is already running. |
| `GET` | `/opportunities` | Current opportunities. Filter by `tradeable_only`, `min_margin`, `resolution_status`. |
| `GET` | `/trades` | Open and settled trades with P&L. |
| `GET` | `/trades/{id}` | One trade with its full event log. |
| `POST` | `/trades/{id}/unwind` | Sell every filled leg at market, now. |
| `GET` | `/positions` | Per-venue exposure, balances and live venue positions. |
| `GET` | `/settings` | Current config; credentials masked. |
| `PUT` | `/settings` | Partial update. Validated, persisted, audited per field. |
| `PUT` | `/settings/credentials` | Write one credential to Secret Manager. |
| `GET` | `/settings/audit` | Settings change history. |
| `POST` | `/kill` | Engage or release the kill switch. |
| `GET` | `/kill` | Kill switch state. |
| `GET` | `/dashboard` | Everything the dashboard needs in one round trip. |
| `GET` | `/history` | Scan archive, spread distribution, resolution match rates. |
| `GET` | `/health` `/ready` `/info` | Liveness, readiness (sidecar up), build info. |

---

## Settings

Everything below is editable in the UI and stored in Firestore. Every change
writes a field-level audit record — what changed, from what, to what, when, by
whom.

- **Venues** — enable/disable, fee model (`none` / `flat_bps` /
  `kalshi_quadratic` with rate), poll priority, markets per scan, max
  exposure, order type, slippage
- **Scanning** — poll interval, min liquidity, min 24h volume, match
  threshold, resolution-window bounds, category and keyword filters,
  book-fetch triage floor and cap
- **Margin** — min net margin to trade, min to record, outcome price bounds,
  complementarity tolerance, plausibility ceiling
- **Execution** — trading on/off, dry run, stake and hard max, max concurrent
  trades, slippage tolerance, second-leg retry limit and reprice ceiling, fill
  timeout and poll interval, unwind retry limit and loss flag, full-depth
  requirement
- **Risk** — kill switch and reason, daily loss limit, daily trade limit, max
  total exposure, required resolution status, UNVERIFIED override and reason
- **Alerts** — unmatched-leg, failed-unwind and error thresholds, plus
  auto-halt after N unmatched legs

**Defaults do not trade.** A fresh deployment scans, records, and displays,
with `trading_enabled` false and `dry_run` true. Dry run runs every pre-trade
check and then submits nothing — it does not invent fills, because a dry run
that fabricated one would report that the execution layer works when it has
never been exercised.

---

## Fee models

A margin quoted before fees is not a margin.

| Model | Formula |
|---|---|
| `none` | 0 |
| `flat_bps` | `shares × price × bps/10000` |
| `kalshi_quadratic` | `ceil(rate × shares × price × (1 − price))`, rounded up to the cent |

Kalshi's fee is quadratic and peaks at 50c — where most interesting arbitrage
sits. At the default 0.07 rate, 100 shares at 50c costs $1.75, which is 3.5%
of notional and enough to erase most cross-venue spreads on its own. Treating
it as a flat percentage understates cost exactly where it matters most.

**The defaults are settings, not facts.** They reflect each venue's published
schedule at the time of writing. Confirm them against the venues before
capital moves — a wrong fee model does not fail loudly, it quietly turns a
positive margin into a negative one.

---

## Repository

```
chimera-predmark/
├── backend/
│   ├── main.py                FastAPI app, all endpoints
│   ├── Dockerfile             python:3.12-slim + node 20, uvicorn on $PORT
│   ├── entrypoint.sh          starts the pmxt sidecar, then uvicorn
│   ├── config.py              infrastructure wiring only
│   ├── models.py              domain model
│   ├── logging_setup.py       structured logs with credential redaction
│   ├── venues/                pmxt client, registry, adapters
│   ├── matching/              text.py, pairing.py, resolution.py
│   ├── margin/                fees.py, calculator.py
│   ├── execution/             orders.py, legs.py, unwind.py, engine.py
│   ├── risk/                  limits.py, killswitch.py, exposure.py
│   ├── settings/              schema.py, store.py, secrets.py
│   ├── storage/               gcs.py, trades.py
│   ├── scan/                  runner.py
│   └── tests/                 93 tests
├── .github/workflows/         deploy-backend.yml
├── NOTICE.md                  third-party licences
└── README.md
```

---

## What Charles needs to set

### GitHub repository secrets

| Secret | What |
|---|---|
| `GCP_WORKLOAD_IDENTITY_PROVIDER` | `projects/<num>/locations/global/workloadIdentityPools/<pool>/providers/<provider>` |
| `GCP_DEPLOY_SERVICE_ACCOUNT` | Service account the workflow impersonates to build and deploy |
| `GCP_RUNTIME_SERVICE_ACCOUNT` | Service account the Cloud Run service runs as |
| `ALLOWED_ORIGINS` | Comma-separated browser origins, e.g. `https://chimerasportstrading.com` |

### GitHub repository variables

| Variable | What |
|---|---|
| `PREDMARK_API_BASE` | API base the dashboard calls. Defaults to `/api/predmark` (the portal proxy path) if unset. |

### Secret Manager

`predmark-pmxt-access-token` — any random string. The six venue credentials
are created by the settings page on first write.

### Runtime service account permissions

Nothing beyond these:

- `roles/datastore.user` — Firestore read/write
- `roles/storage.objectAdmin` on `predmark-data` — GCS read/write
- `roles/secretmanager.secretAccessor` — read credentials at execution time
- `roles/secretmanager.admin` **scoped to the `predmark-*` secrets** —
  the settings page needs create and add-version. Scope it to those secrets
  rather than the project; `secretAccessor` alone cannot create a secret, and
  project-wide `admin` is more than this service should hold.

### Cloud Scheduler

Job `predmark-scan`, `europe-west1`:

```
gcloud scheduler jobs create http predmark-scan \
  --project chiops --location europe-west1 \
  --schedule "*/5 * * * *" \
  --uri "https://<cloud-run-url>/scan" \
  --http-method POST \
  --oidc-service-account-email <invoker-service-account> \
  --oidc-token-audience "https://<cloud-run-url>" \
  --attempt-deadline 900s
```

Keep the schedule and `scanning.poll_interval_seconds` in step — the setting
only drives the "next scan" display.

### Cloud Run

Set by the workflow, listed so it is on the record: `--no-allow-unauthenticated`,
2 CPU, 2 GiB, timeout 900s, concurrency 4, `min-instances 0`, `max-instances 2`.

---

## Local development

Nothing here is required to deploy — the container is the artefact.

```bash
cd backend
pip install -r requirements.txt && pip install pytest pytest-asyncio
python -m pytest -q                     # 93 tests, no GCP credentials needed

# Against live venues, no credentials required for reads:
npm install pmxt-core@2.54.0
PMXT_ACCESS_TOKEN=dev node node_modules/pmxt-core/dist/server/index.js &
PMXT_ACCESS_TOKEN=dev GCP_PROJECT=chiops uvicorn main:app --port 8080
```

The full container:

```bash
docker build -t predmark backend/
docker run -p 8080:8080 -e PMXT_ACCESS_TOKEN=dev predmark
```

---

## The dashboard

The UI is not in this repository. It lives in the CST portal
(`chimeracloud/cst`) as four React pages under
`/products/arbitrage/predmark/`:

| Page | What it shows |
|---|---|
| Dashboard | Live opportunities with both venues' resolution criteria side by side, open positions, venue balances, kill switch, scan trigger |
| Trades | Both legs, fills, settlement, realised P&L, unmatched-leg and failed-unwind flags |
| History | Spread distribution, resolution match rates, P&L by day, scan archive |
| Settings | Every configurable value, credential entry, and the settings audit trail |

It reaches this service through the portal proxy —
`cst-api` → `/api/proxy/predmark/...` → Cloud Run — which is why the service
can stay `--no-allow-unauthenticated` and still be usable from a browser.

A standalone Cloudflare Pages site was built first and has been removed. It sat
on its own origin with no identity, so every settings change was audited as
"dashboard" with nobody's name against it. Inside the portal the same page
inherits Cloudflare Access and the portal JWT, and a page that can halt
trading, change stake, store wallet keys and force an unwind is attributable
to a person.

---

## Decisions taken where the brief was open

Noted as instructed, rather than stopping to ask.

1. **Firestore `predmark-config` and `predmark-trades` are collections** in the
   project's default database, not separate named databases. A single service
   cannot hold two default database handles cleanly, and collections give the
   same separation. `FIRESTORE_DATABASE` overrides the database if Charles
   provisions a named one.

2. **pmxt is installed from npm at a pinned version** (`2.54.0`) rather than
   vendored as a git clone. It is the same MIT source, self-hosted, and
   pinning means a venue integration cannot change under a running trading
   system. The clone in `reference/` was read to verify the sidecar contract.

3. **The sidecar shares the API container** rather than running as a Cloud Run
   multi-container sidecar. One image, one deploy, one lifecycle, and the
   sidecar is unreachable from outside the container.

4. **Opportunities are a live view, not a ledger.** Each scan replaces the
   `predmark-opportunities` collection; the durable record of every scan is
   the GCS archive. A dashboard showing a spread that closed twenty minutes
   ago is worse than showing none.

5. **Book fetches are triaged.** Pairs are screened on top-of-book prices
   already present in the market payload, and only those clearing
   `book_fetch_margin_floor` are priced against a real ladder, capped at
   `max_book_fetches`. A book fetch per candidate pair per scan would be slow
   and rude to the venues. Nothing is ever *traded* on a triage price.

6. **Kalshi's order book needs credentials.** Verified live against the
   sidecar: `fetchOrderBook` returns 401 to an anonymous caller, while the
   market payload carries top-of-book bid/ask with sizes. Uncredentialed
   scans therefore get `depth_source: "top_of_book"` for Kalshi, and the
   pre-trade check blocks on `depth_source: "none"`. Credentials are attached
   to read calls too, so a configured deployment gets the real ladder.

7. **Buying NO consumes the YES bid.** Kalshi quotes one book; buying NO at
   *p* is selling YES at *1 − p*. The adapter mirrors sizes accordingly.
   Reading the YES ask instead would overstate depth on the exact leg being
   taken — by 8x in the payload used as a test fixture.

8. **Polymarket's upstream rejects `status` and `sort`.** Verified live: those
   parameters return HTTP 422 through pmxt while a bare `{limit}` fetch
   succeeds and comes back volume-ordered. `fetch_markets` degrades through
   three progressively simpler parameter sets rather than losing the venue.

9. **Liquidity falls back to 24h volume.** Kalshi reports `liquidity: 0` on
   every market. A naive floor would silently exclude the entire venue, which
   looks like "no opportunities" rather than "a filter is wrong".

10. **Binary markets only.** Markets with other than two outcomes are skipped.
    Both venues already explode categorical events into one binary market per
    candidate, so this discards nothing tradeable.

11. **Only the excess is unwound on unequal partial fills.** If leg A fills
    100 and leg B fills 60, sixty shares are genuinely hedged and are kept;
    the 40-share excess is exposure and goes.

12. **The unwind loss tolerance flags, it does not gate.** A setting that
    could refuse to close an unhedged position would turn a small loss into an
    open-ended one.

13. **Actor identity is whatever the proxy forwards.** Audit records take the
    authenticated-user header if present and record `dashboard` otherwise. An
    honest "unknown" beats a fabricated username — see the second note below.

14. **Liquidity floors are per venue**, falling back to a global setting.
    Polymarket publishes liquidity in the millions; Kalshi publishes `0` on
    every market and its entire active book turned over $2.6k in the 24h
    window this was built against. One global floor either excludes Kalshi
    completely or lets Polymarket's dust through. Kalshi defaults to a volume
    floor of $100 and no liquidity floor.

15. **`max_days_to_resolution` defaults to three years, not one.** The liquid
    Kalshi book is largely 2028 elections and the 2030 World Cup; a 365-day
    cap excluded roughly nine tenths of it. Capital lock-up is a cost to
    price into the margin floor, not a reason to be blind to the market.

16. **Polymarket's compound titles are split.** Its titles are
    `"Event name - Question?"` while Kalshi states the question alone.
    Scored whole, a genuine match lands near 0.5 and is discarded; scored on
    the question part, 1.0. Similarity takes the better of the two.

---

## What live testing changed

The build was developed against the real venues throughout, and four things
that looked fine on paper turned out to be wrong. They are called out because
each is a way this could have lost money quietly.

**Limitless serves the same order book for both outcomes.** Verified directly:
"Will China invade Taiwan by end of 2026?" returned an identical ladder for
the YES and NO tokens despite distinct outcome IDs, so both quoted 0.044. The
scan duly reported a **+1090% margin** against Polymarket — on identical
question text, with resolution assessed `MATCHED` and real depth on both
sides. Every check the brief asks for passed.

Two defences now exist, neither of which trusts the venue:

- *Complementarity.* A binary market's two outcomes must price to about $1
  between them, because holding both pays exactly $1. That is arithmetic, not
  a market view. Markets breaching it by more than `complement_tolerance` are
  dropped — which removed 61 of 67 Limitless markets on the next run, and
  both phantoms with them.
- *A plausibility ceiling.* Real cross-venue spreads run to a few per cent.
  Anything above `max_plausible_margin` (default 35%) is recorded and
  displayed but never traded, because at that level a stale quote or a
  mis-mapped outcome is overwhelmingly likelier than free money. Enforced in
  the pre-trade checks, not just the scanner.

**Competing candidates scored 0.81 and paired.** "Will Italy win the 2030
World Cup?" against "Will England win the 2030 World Cup?" — near-identical
prose, sharing the token "FIFA". Buying Italy YES against England NO is not a
hedge; both legs can lose. Pairing now requires that neither side names a
distinctive entity the other lacks. One-sided extra context is still fine, so
"BTC above $100k" still pairs with "BTC above $100k on Binance".

**Date fragments defeated the threshold guard.** "Above $100,000 on December
31, 2026" and "above $120,000 on December 31, 2026" both extract
`{31, 2026, 100000}` and `{31, 2026, 120000}`; an overlap test found `{31,
2026}` in common and paired two entirely different bets. Calendar components
are now stripped before thresholds are compared, and the comparison is
equality rather than overlap.

**Kalshi's fee rounded a cent high.** `0.07 × 100 × 0.5 × 0.5 × 100` evaluates
to `175.00000000000003` in binary floating point, and an unguarded `ceil`
turned an exact $1.75 into $1.76. Small — but it is a fee model, and one that
is wrong in one direction is wrong.

### Current market conditions, for what they are worth

At the time of writing, a full three-venue scan finds **no valid cross-venue
pairs**. Polymarket's liquid book is sports and foreign elections; Kalshi's
active universe is 1,493 markets of which 37 have more than $100 of 24h
volume; Limitless's outcome data is mostly unusable for the reason above.
That is a genuine finding rather than a gap in the code — the matcher, the
resolution engine and the margin calculation all run correctly and correctly
conclude there is nothing to trade. The counters and the history view exist
so that this stays visible.

---

## For Charles — two things before funding

**1. Exchange control.** Funding these venues with USDC means converting rand
to crypto, which engages South African exchange control. Worth confirming the
position with the bank or an adviser before capital moves, not after. Nothing
in this repository depends on the answer, but the funding step does.

**2. The dashboard can now move money.** It currently sits behind a single
Cloudflare WAF IP rule — no identity, no audit trail, and the IP is dynamic.
That is acceptable for a read-only portal. This page has a kill switch, a
stake field, a credential form and a manual unwind button, so the WAF rule is
now the only thing between a passing stranger on the same IP and the trading
configuration.

Two things partly compensate and neither closes the gap: the Cloud Run service
is `--no-allow-unauthenticated` so the proxy is the only route in, and every
settings change is audited — but audited *as* `dashboard`, because there is no
identity to record. Worth revisiting alongside the identity work, and worth
doing before the stake moves beyond what you would shrug at.
