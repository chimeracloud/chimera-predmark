# Third-party notices

Licences were checked before anything was ported.

## Used and adapted

**pmxt** — https://github.com/pmxt-dev/pmxt — MIT, Copyright (c) 2026 pmxt.dev

Run as `pmxt-core` from npm, pinned in `backend/Dockerfile`. Self-hosted
inside the container; the hosted pmxt.dev service is not in the request path.
Unmodified, so the MIT licence travels with the package in `node_modules`.

**prediction-market-arbitrage-bot** — https://github.com/realfishsam/prediction-market-arbitrage-bot
— MIT, Copyright (c) 2026 Samuel EF. Tinnerholm

The Jaccard + Levenshtein blended title similarity and the greedy one-to-one
pairing in `backend/matching/text.py` and `backend/matching/pairing.py` are
adapted from `src/matcher.js`. The two-strategy enumeration in
`backend/margin/calculator.py` follows the shape of `src/arbitrage.js`.

What was rewritten rather than adapted, and why:

- Fees. The original computes gross profit as `100 - (priceA + priceB)` in
  cents with no fee model at all. Kalshi's quadratic fee alone erases most
  cross-venue spreads.
- Depth. The original prices against `outcome.price`, the venue's headline
  probability, rather than the ask ladder — so the margin it reports is the
  margin available for one share.
- Single-leg fills. The original submits both legs with `Promise.all` and, on
  a failure, logs `[ERROR] Trade execution failed` and returns `false`,
  leaving any filled leg standing as an unhedged position. Containing that is
  the whole subject of `backend/execution/legs.py`.
- Resolution criteria. Not addressed upstream; markets are paired on title
  similarity alone.
- Sizing. The original's YOLO mode goes all-in with available capital.
- State. The original keeps positions in memory and runs on `setInterval`.

**prediction-market-analysis** — https://github.com/Jon-Becker/prediction-market-analysis
— MIT, Copyright (c) 2026 Jonathan Becker

Read for its venue data models. No code ported.

## Read but not ported

These two repositories carry **no licence file**, so all rights are reserved
by their authors and nothing from them may be copied. Both were read for
understanding only, and no code, structure, or comment from either appears in
this repository:

- **polymarket-arbitrage** — https://github.com/ImMike/polymarket-arbitrage
- **prediction-markets-arbitrage-scan** — https://github.com/realfishsam/prediction-markets-arbitrage-scan

Where this codebase does something those repositories also do — walking an
order book to a VWAP, for instance — it was written from the arithmetic
rather than from their source.
