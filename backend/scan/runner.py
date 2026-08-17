"""The scan cycle.

One invocation, one complete pass: poll venues, pair equivalent markets,
assess resolution equivalence, price against real depth net of fees, record
everything, then hand whatever qualifies to the execution engine.

Invoked by Cloud Scheduler rather than run from an in-process loop. The lay
engine's background loop kept a container alive indefinitely, so an expired
credential was never refreshed, and that took the platform down three times.
A stateless service invoked on a schedule reads its credentials fresh on
every cycle and cannot develop that failure.

Cost control matters here: a full pass over three venues is thousands of
candidate pairs, and an order book fetch each would be both slow and rude to
the venues. So pairs are triaged on top-of-book prices, which are already in
the market payload, and only the survivors are priced properly.
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from itertools import combinations
from typing import Any, Optional

from execution import engine as execution_engine
from logging_setup import log
from margin import calculator
from matching import pairing, resolution
from models import (
    Market,
    Opportunity,
    OrderBook,
    Outcome,
    ResolutionStatus,
    new_id,
)
from risk import limits
from settings import secrets
from settings.schema import Settings
from settings.store import bump_state, is_killed, load_state, record_scan
from storage import gcs
from storage import trades as trade_store
from venues.adapters import VenueAdapter
from venues.pmxt_client import PmxtClient

logger = logging.getLogger(__name__)


@dataclass
class ScanResult:
    scan_id: str
    started_at: datetime
    duration_seconds: float = 0.0
    markets_by_venue: dict[str, int] = field(default_factory=dict)
    pairs_considered: int = 0
    pairs_priced: int = 0
    books_fetched: int = 0
    opportunities: list[Opportunity] = field(default_factory=list)
    trades: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    archive_uri: Optional[str] = None
    trading_attempted: bool = False

    def summary(self) -> dict[str, Any]:
        by_status: dict[str, int] = {}
        for opportunity in self.opportunities:
            key = opportunity.resolution.status.value
            by_status[key] = by_status.get(key, 0) + 1
        return {
            "scan_id": self.scan_id,
            "started_at": self.started_at.isoformat(),
            "duration_seconds": round(self.duration_seconds, 3),
            "markets_by_venue": self.markets_by_venue,
            "markets_total": sum(self.markets_by_venue.values()),
            "pairs_considered": self.pairs_considered,
            "pairs_priced": self.pairs_priced,
            "books_fetched": self.books_fetched,
            "opportunities_found": len(self.opportunities),
            "opportunities_tradeable": len(
                [o for o in self.opportunities if o.tradeable]
            ),
            "by_resolution_status": by_status,
            "trades_placed": len(self.trades),
            "trade_ids": self.trades,
            "trading_attempted": self.trading_attempted,
            "errors": self.errors,
            "archive_uri": self.archive_uri,
        }


async def _fetch_venue_markets(
    client: PmxtClient, settings: Settings, venue: str
) -> tuple[str, list[Market]]:
    """Markets for one venue, credentialed where possible.

    Credentials are attached even for reads because Kalshi will not serve an
    order book without them.
    """
    credentials = secrets.load_credentials(venue)
    adapter = VenueAdapter(venue, client, credentials or None)
    venue_settings = settings.venues[venue]
    try:
        markets = await adapter.fetch_markets(limit=venue_settings.market_limit)
    except Exception as exc:  # noqa: BLE001 - one venue must not fail the scan
        log(logger, logging.ERROR, "venue scan failed", venue=venue, error=str(exc))
        return venue, []
    return venue, markets


def _eligible(market: Market, settings: Settings, now: datetime) -> bool:
    """Whether a market is worth pairing at all.

    Liquidity floors are resolved per venue before falling back to the global
    setting, because the venues do not report on a comparable scale.
    """
    scanning = settings.scanning
    venue_settings = settings.venues.get(market.venue)

    min_liquidity = scanning.min_liquidity
    min_volume = scanning.min_volume_24h
    if venue_settings:
        if venue_settings.min_liquidity is not None:
            min_liquidity = venue_settings.min_liquidity
        if venue_settings.min_volume_24h is not None:
            min_volume = venue_settings.min_volume_24h

    if market.effective_liquidity < min_liquidity:
        return False
    if market.volume_24h < min_volume:
        return False
    if len(market.outcomes) != 2:
        return False

    # Every outcome needs a usable price.
    if any(o.price <= 0 or o.price >= 1 for o in market.outcomes):
        return False

    # The two outcomes must price to about $1 between them. Holding both pays
    # exactly $1, so this is arithmetic rather than a market view, and a venue
    # that breaches it is publishing data we cannot price against. Live, this
    # rejects the Limitless markets that return the same order book for both
    # outcomes — which otherwise present as enormous, entirely fictional
    # arbitrage.
    complement = sum(o.price for o in market.outcomes)
    if abs(complement - 1.0) > settings.margin.complement_tolerance:
        return False

    if market.resolution_date:
        hours = (market.resolution_date - now).total_seconds() / 3600.0
        # A market resolving imminently cannot be entered on two venues
        # safely; one resolving years out ties up capital for nothing.
        if hours < scanning.min_hours_to_resolution:
            return False
        if hours > scanning.max_days_to_resolution * 24:
            return False

    title = market.title.lower()
    if any(keyword.lower() in title for keyword in scanning.exclude_keywords):
        return False
    category = (market.category or "").lower()
    if scanning.include_categories and category not in {
        c.lower() for c in scanning.include_categories
    }:
        return False
    if category and category in {c.lower() for c in scanning.exclude_categories}:
        return False
    return True


def _ask_of(outcome: Outcome) -> Optional[float]:
    """Best executable buy price, falling back to the headline probability.

    The headline price is a mid or last-trade on some venues, so it is only a
    triage input — nothing is ever traded on it. The real ask comes from the
    book fetch that follows.
    """
    if outcome.ask and 0 < outcome.ask < 1:
        return outcome.ask
    if 0 < outcome.price < 1:
        return outcome.price
    return None


async def run_scan(
    client: PmxtClient,
    settings: Settings,
    execute: bool = True,
) -> ScanResult:
    """One complete detect-decide-execute cycle."""
    scan_id = new_id("scan")
    started = datetime.now(timezone.utc)
    clock = time.perf_counter()
    result = ScanResult(scan_id=scan_id, started_at=started)

    venues = settings.enabled_venues()
    if len(venues) < 2:
        result.errors.append(
            f"{len(venues)} venue(s) enabled — cross-venue arbitrage needs at least two"
        )
        result.duration_seconds = time.perf_counter() - clock
        record_scan(scan_id, started)
        return result

    # 1. Poll every enabled venue concurrently.
    fetched = await asyncio.gather(
        *(_fetch_venue_markets(client, settings, venue) for venue in venues)
    )
    markets_by_venue: dict[str, list[Market]] = {}
    for venue, markets in fetched:
        eligible = [m for m in markets if _eligible(m, settings, started)]
        markets_by_venue[venue] = eligible
        result.markets_by_venue[venue] = len(eligible)
        log(
            logger,
            logging.INFO,
            "venue polled",
            venue=venue,
            fetched=len(markets),
            eligible=len(eligible),
        )

    # 2. Pair, assess resolution, triage on price.
    triaged: list[tuple[pairing.PairCandidate, Outcome, Outcome, float, Any]] = []
    for venue_a, venue_b in combinations(venues, 2):
        pairs = pairing.find_pairs(
            markets_by_venue[venue_a],
            markets_by_venue[venue_b],
            threshold=settings.scanning.match_threshold,
        )
        result.pairs_considered += len(pairs)

        fees_a = settings.venues[venue_a].fee_model
        fees_b = settings.venues[venue_b].fee_model

        for pair in pairs:
            pairing.align_outcomes(pair)
            if not pair.complements:
                continue
            assessment = resolution.compare(pair.market_a, pair.market_b)

            for outcome_a, outcome_b in pair.complements:
                ask_a, ask_b = _ask_of(outcome_a), _ask_of(outcome_b)
                headline = calculator.headline_margin_from_prices(
                    ask_a, fees_a, ask_b, fees_b
                )
                if headline < settings.scanning.book_fetch_margin_floor:
                    continue
                triaged.append((pair, outcome_a, outcome_b, headline, assessment))

    # Best-looking candidates first, so a capped scan spends its book
    # fetches where they are most likely to pay for themselves.
    triaged.sort(key=lambda item: item[3], reverse=True)
    triaged = triaged[: settings.scanning.max_book_fetches]

    # 3. Price the survivors against real depth.
    opportunities: list[Opportunity] = []
    book_cache: dict[tuple[str, str], OrderBook] = {}
    adapters: dict[str, VenueAdapter] = {
        venue: VenueAdapter(venue, client, secrets.load_credentials(venue) or None)
        for venue in venues
    }

    async def book_for(market: Market, outcome: Outcome) -> OrderBook:
        key = (market.venue, outcome.outcome_id)
        if key not in book_cache:
            book_cache[key] = await adapters[market.venue].fetch_order_book(
                market, outcome
            )
            result.books_fetched += 1
        return book_cache[key]

    stake = settings.effective_stake()
    for pair, outcome_a, outcome_b, headline, assessment in triaged:
        try:
            book_a, book_b = await asyncio.gather(
                book_for(pair.market_a, outcome_a),
                book_for(pair.market_b, outcome_b),
            )
        except Exception as exc:  # noqa: BLE001 - one book must not fail the scan
            result.errors.append(f"book fetch failed: {exc}")
            continue

        result.pairs_priced += 1
        fees_a = settings.venues[pair.market_a.venue].fee_model
        fees_b = settings.venues[pair.market_b.venue].fee_model

        priced = calculator.best_execution(
            pair.market_a,
            outcome_a,
            book_a,
            fees_a,
            pair.market_b,
            outcome_b,
            book_b,
            fees_b,
            max_stake=stake,
            min_margin=settings.margin.min_margin_to_trade,
        )
        if not priced or priced.shares <= 0:
            continue
        if priced.net_margin < settings.margin.min_margin_to_record:
            continue

        opportunity = Opportunity(
            scan_id=scan_id,
            leg_a=priced.leg_a,
            leg_b=priced.leg_b,
            shares=priced.shares,
            gross_cost=priced.gross_cost,
            total_fees=priced.total_fees,
            total_cost=priced.total_cost,
            payout=priced.payout,
            profit=priced.profit,
            net_margin=priced.net_margin,
            headline_margin=headline,
            match_score=pair.score,
            match_notes=list(pair.notes),
            resolution=assessment,
        )

        # Implausible margins are data errors, not opportunities. Recording
        # them rather than discarding them matters: a run of these is the
        # signal that a venue feed has broken, and silently dropping them
        # would hide exactly the thing worth knowing.
        if opportunity.net_margin > settings.margin.max_plausible_margin:
            opportunity.blocked_reasons.append(
                f"implausible margin {opportunity.net_margin:.2%} exceeds the "
                f"{settings.margin.max_plausible_margin:.0%} ceiling — treated as a "
                "data error, not an opportunity"
            )
            opportunity.match_notes.append("flagged: implausible margin")

        opportunities.append(opportunity)

    opportunities.sort(key=lambda o: o.net_margin, reverse=True)
    result.opportunities = opportunities
    log(
        logger,
        logging.INFO,
        "scan priced",
        scan_id=scan_id,
        pairs=result.pairs_considered,
        priced=result.pairs_priced,
        opportunities=len(opportunities),
    )

    # 4. Decide and execute.
    if execute:
        await _execute_qualifying(client, settings, opportunities, result)
    else:
        for opportunity in opportunities:
            opportunity.tradeable = False
            opportunity.blocked_reasons = ["execution skipped for this scan"]

    # 5. Persist: current view to Firestore, full record to GCS.
    trade_store.replace_opportunities(
        scan_id, [o.to_dict() for o in opportunities[:200]]
    )
    result.duration_seconds = time.perf_counter() - clock

    payload = {
        "_at": started,
        "scan": result.summary(),
        "settings_snapshot": {
            "margin": settings.margin.model_dump(),
            "scanning": settings.scanning.model_dump(),
            "execution": settings.execution.model_dump(),
            "risk": settings.risk.model_dump(),
            "venues": {
                name: venue.model_dump() for name, venue in settings.venues.items()
            },
        },
        "opportunities": [o.to_dict() for o in opportunities],
    }
    result.archive_uri = gcs.write_scan(scan_id, payload)
    record_scan(scan_id, started)
    if result.errors:
        bump_state(errors_today=len(result.errors))

    return result


async def _execute_qualifying(
    client: PmxtClient,
    settings: Settings,
    opportunities: list[Opportunity],
    result: ScanResult,
) -> None:
    """Run each opportunity through the checks, trading those that pass.

    The kill switch is read fresh from Firestore here, immediately before any
    execution decision, so flipping it in the UI stops the very next trade.
    """
    killed = is_killed()
    if killed or not settings.execution.trading_enabled:
        reason = (
            "kill switch engaged" if killed else "trading disabled in settings"
        )
        for opportunity in opportunities:
            opportunity.tradeable = False
            opportunity.blocked_reasons = [reason]
        log(logger, logging.INFO, "execution skipped", reason=reason)
        return

    result.trading_attempted = True
    state = load_state()
    from risk import exposure as exposure_module

    balances = await exposure_module.fetch_balances(client, settings)
    credentialed = {
        venue for venue in settings.enabled_venues() if secrets.credentials_ready(venue)
    }

    context = limits.RiskContext(
        settings=settings,
        killed=killed,
        open_trade_count=trade_store.count_open_trades(),
        trades_today=int(state.get("trades_today") or 0),
        realised_pnl_today=trade_store.realised_pnl_today(),
        unmatched_legs_today=int(state.get("unmatched_legs_today") or 0),
        exposure_by_venue=trade_store.exposure_by_venue(),
        balances=balances,
        credentialed_venues=credentialed,
    )

    for opportunity in opportunities:
        # Re-read the switch between trades: a scan can place several, and
        # "immediately" has to mean immediately.
        if is_killed():
            opportunity.tradeable = False
            opportunity.blocked_reasons = ["kill switch engaged mid-scan"]
            log(logger, logging.WARNING, "kill switch engaged mid-scan, stopping")
            break

        decision = limits.evaluate(opportunity, context)
        opportunity.tradeable = decision.allowed
        opportunity.blocked_reasons = decision.reasons
        if not decision.allowed:
            continue

        try:
            trade = await execution_engine.execute_opportunity(
                opportunity, client, settings, context
            )
        except Exception as exc:  # noqa: BLE001 - a failed trade must not stop the scan
            result.errors.append(f"execution failed for {opportunity.id}: {exc}")
            continue
        if trade:
            result.trades.append(trade.id)


async def settle_open_trades(client: PmxtClient, settings: Settings) -> dict[str, Any]:
    """Check open trades for resolution and book the P&L.

    A hedged pair pays out $1 per share on exactly one side. Settlement is
    detected by the position disappearing from the venue, which is how both
    venues report a resolved market.
    """
    from venues.pmxt_client import PmxtError

    open_records = [
        t for t in trade_store.open_trades() if t.get("status") == "OPEN"
    ]
    if not open_records:
        return {"checked": 0, "settled": 0}

    settled = 0
    now = datetime.now(timezone.utc)
    for raw in open_records:
        trade = execution_engine._rehydrate(raw)
        created = raw.get("created_at") or ""
        # Nothing resolves within the hour; skip the pointless venue calls.
        try:
            if created and datetime.fromisoformat(created) > now - timedelta(hours=1):
                continue
        except ValueError:
            pass

        adapters = await execution_engine.build_adapters(client, trade.venues)
        still_open = False
        payout = 0.0
        for leg in trade.legs:
            adapter = adapters.get(leg.venue)
            if not adapter or leg.filled_shares <= 0:
                continue
            try:
                positions = await adapter.fetch_positions()
            except PmxtError:
                still_open = True
                continue
            held = next(
                (p for p in positions if p.get("outcome_id") == leg.outcome_id), None
            )
            if held and abs(float(held.get("size") or 0)) > 1e-9:
                still_open = True
            else:
                # Gone from the venue: the market resolved. A winning leg
                # returns $1 per share, a losing one returns nothing, and the
                # venue's realised P&L tells us which.
                realised = float(held.get("realised_pnl") or 0) if held else 0.0
                payout += leg.cost + realised if realised else 0.0
                leg.settled_at = now

        if still_open:
            continue

        # One side of a hedge always pays $1 per share.
        hedged_shares = min(
            (leg.filled_shares for leg in trade.legs if leg.filled_shares > 0),
            default=0.0,
        )
        gross_payout = hedged_shares
        trade.realised_pnl = round(gross_payout - trade.actual_cost, 6)
        trade.status = trade.status.__class__.SETTLED
        trade.settlement_notes = (
            f"resolved: {hedged_shares} shares paid out {gross_payout:.4f} "
            f"against cost {trade.actual_cost:.4f}"
        )
        trade.record("settled", payout=gross_payout, pnl=trade.realised_pnl)
        trade_store.save_trade(trade)
        bump_state(realised_pnl_today=trade.realised_pnl or 0.0)
        settled += 1

    return {"checked": len(open_records), "settled": settled}
