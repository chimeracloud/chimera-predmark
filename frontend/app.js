/* Chimera PredMark — dashboard.
 *
 * Reads from the Cloud Run service through the portal proxy. The backend is
 * deployed --no-allow-unauthenticated, so API_BASE points at the proxy path
 * rather than the run.app URL directly.
 *
 * Refreshes every 30 seconds. Refresh pauses while the tab is hidden: a
 * dashboard left open overnight should not keep a scanner's worth of
 * Firestore reads running against nobody.
 */

const API_BASE = (window.PREDMARK_API || "/api/predmark").replace(/\/$/, "");
const REFRESH_MS = 30000;

let refreshTimer = null;
let currentTab = "dashboard";
let lastDashboard = null;

/* --- helpers ---------------------------------------------------------- */

async function api(path, options) {
    const response = await fetch(`${API_BASE}${path}`, {
        headers: { "content-type": "application/json" },
        ...options,
    });
    if (!response.ok) {
        let detail = response.statusText;
        try {
            const body = await response.json();
            detail = body.detail || detail;
        } catch (_) { /* keep the status text */ }
        throw new Error(`${response.status}: ${detail}`);
    }
    return response.json();
}

const usd = (value, dp = 2) =>
    value === null || value === undefined || Number.isNaN(Number(value))
        ? "—"
        : `$${Number(value).toFixed(dp)}`;

const pct = (value, dp = 2) =>
    value === null || value === undefined || Number.isNaN(Number(value))
        ? "—"
        : `${(Number(value) * 100).toFixed(dp)}%`;

const num = (value, dp = 1) =>
    value === null || value === undefined || Number.isNaN(Number(value))
        ? "—"
        : Number(value).toFixed(dp);

const clock = (iso) => {
    if (!iso) return "—";
    const date = new Date(iso);
    return Number.isNaN(date.getTime()) ? "—" : date.toLocaleString();
};

const ago = (iso) => {
    if (!iso) return "never";
    const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
    if (Number.isNaN(seconds)) return "—";
    if (seconds < 60) return `${Math.round(seconds)}s ago`;
    if (seconds < 3600) return `${Math.round(seconds / 60)}m ago`;
    return `${Math.round(seconds / 3600)}h ago`;
};

const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);

const signClass = (value) => (Number(value) > 0 ? "good" : Number(value) < 0 ? "bad" : "");

function tile(label, value, sub, cls) {
    return `<div class="tile">
        <div class="label">${escapeHtml(label)}</div>
        <div class="value ${cls || ""}">${value}</div>
        ${sub ? `<div class="sub">${sub}</div>` : ""}
    </div>`;
}

function badge(text, cls) {
    return `<span class="badge ${cls || ""}">${escapeHtml(text)}</span>`;
}

function bars(container, entries, total) {
    const max = Math.max(...entries.map(([, v]) => v), 1);
    container.innerHTML = entries
        .map(([label, value]) => {
            const width = (value / max) * 100;
            const share = total ? ` (${((value / total) * 100).toFixed(0)}%)` : "";
            return `<div class="bar-row">
                <span class="bar-label">${escapeHtml(label)}</span>
                <span class="bar-track"><span class="bar-fill" style="width:${width}%"></span></span>
                <span class="bar-value">${value}${share}</span>
            </div>`;
        })
        .join("");
}

/* --- kill switch ------------------------------------------------------ */

function renderKillBar(status) {
    const bar = document.getElementById("kill-bar");
    const state = document.getElementById("kill-state");
    const detail = document.getElementById("kill-detail");
    const button = document.getElementById("kill-button");

    if (status.kill_switch) {
        bar.classList.add("engaged");
        state.className = "state halted";
        state.textContent = "EXECUTION HALTED";
        detail.textContent = status.kill_switch_reason
            ? `— ${status.kill_switch_reason}. Scanning continues.`
            : "— scanning continues.";
        button.textContent = "Release halt";
        button.classList.remove("danger");
    } else {
        bar.classList.remove("engaged");
        const live = status.trading_enabled && !status.dry_run;
        state.className = `state ${live ? "live" : ""}`;
        state.textContent = status.trading_enabled
            ? (status.dry_run ? "DRY RUN — no orders sent" : "TRADING LIVE")
            : "TRADING DISABLED";
        detail.textContent = `stake ${usd(status.stake_per_trade)} · floor ${pct(status.min_margin_to_trade)} · ${status.required_resolution_status} · ${(status.enabled_venues || []).join(", ") || "no venues"}`;
        button.textContent = "Halt execution";
        button.classList.add("danger");
    }
}

async function toggleKill() {
    const engaged = lastDashboard?.status?.kill_switch;
    const verb = engaged ? "Release the halt" : "Halt all execution";
    const reason = window.prompt(`${verb}. Reason for the audit record:`, engaged ? "resuming" : "manual halt");
    if (reason === null) return;

    const button = document.getElementById("kill-button");
    button.disabled = true;
    try {
        await api("/kill", {
            method: "POST",
            body: JSON.stringify({ engaged: !engaged, reason }),
        });
        await refresh();
    } catch (error) {
        window.alert(`Kill switch failed: ${error.message}`);
    } finally {
        button.disabled = false;
    }
}

/* --- dashboard -------------------------------------------------------- */

function renderStatusStrip(data) {
    const pnl = data.pnl?.totals || {};
    const today = data.pnl?.today || {};
    const state = data.state || {};
    const exposure = data.exposure || {};

    const tiles = [
        tile("Today's P&L", usd(state.realised_pnl_today ?? today.realised_pnl ?? 0),
             `${today.trades || 0} trade(s) today`, signClass(state.realised_pnl_today ?? 0)),
        tile("Open positions", data.open_trades?.length ?? 0,
             `${usd(exposure.total_committed)} committed`),
        tile("Opportunities", data.opportunities?.length ?? 0,
             `${data.opportunities?.filter((o) => o.tradeable).length ?? 0} tradeable`, "gold"),
        tile("Unmatched legs", state.unmatched_legs_today ?? 0,
             `${state.unmatched_legs_total ?? 0} all time`,
             (state.unmatched_legs_today ?? 0) > 0 ? "bad" : "good"),
        tile("Failed unwinds", state.failed_unwinds_today ?? 0,
             `${pnl.exposed || 0} trade(s) exposed`,
             (state.failed_unwinds_today ?? 0) > 0 ? "bad" : "good"),
        tile("Exposure", usd(exposure.total_committed),
             `cap ${usd(exposure.max_total_exposure)}`),
        tile("Last scan", ago(data.status?.last_scan_at),
             clock(data.status?.last_scan_at)),
        tile("Next scan", data.status?.next_scan_at
                ? new Date(data.status.next_scan_at * 1000).toLocaleTimeString()
                : "—",
             `every ${Math.round((data.status?.poll_interval_seconds || 0) / 60)}m`),
    ];
    document.getElementById("status-strip").innerHTML = tiles.join("");
}

function renderAlerts(alerts) {
    const container = document.getElementById("alerts");
    if (!alerts || !alerts.length) {
        container.innerHTML = "";
        return;
    }
    container.innerHTML = alerts
        .map((a) => `<div class="alert ${a.level}">${escapeHtml(a.message)}</div>`)
        .join("");
}

function legCell(leg) {
    if (!leg) return "—";
    return `<div class="mono">${escapeHtml(leg.venue)}</div>
        <div class="muted small">${escapeHtml(leg.outcome_label)} @ ${num(leg.avg_price, 4)}</div>
        <div class="muted small">depth ${num(leg.depth_available, 0)}${leg.depth_source === "top_of_book" ? " (top only)" : ""}</div>`;
}

function resolutionCell(opportunity) {
    const resolution = opportunity.resolution || {};
    const status = opportunity.resolution_status || "UNVERIFIED";
    const checks = Object.entries(resolution.checks || {})
        .map(([key, verdict]) => `${key}: ${verdict}`)
        .join("\n");

    return `${badge(status, status.toLowerCase())}
        <details class="criteria">
            <summary>criteria</summary>
            <div class="body">
                <div class="side"><b>Checks</b>\n${escapeHtml(checks)}</div>
                <div class="side"><b>Notes</b>\n${escapeHtml(resolution.resolution_notes || "")}</div>
                <div class="side"><b>${escapeHtml(opportunity.leg_a?.venue || "A")}</b>\n${escapeHtml(resolution.resolution_a || "(none published)")}</div>
                <div class="side"><b>${escapeHtml(opportunity.leg_b?.venue || "B")}</b>\n${escapeHtml(resolution.resolution_b || "(none published)")}</div>
            </div>
        </details>`;
}

function renderOpportunities(opportunities) {
    const body = document.querySelector("#opportunities tbody");
    document.getElementById("opp-count").textContent =
        `${opportunities.length} shown`;

    if (!opportunities.length) {
        body.innerHTML = `<tr><td colspan="10" class="empty">No opportunities in the last scan.</td></tr>`;
        return;
    }

    body.innerHTML = opportunities
        .map((o) => {
            const blocked = (o.blocked_reasons || []).join("; ");
            return `<tr>
                <td class="title-cell">
                    <div>${escapeHtml(o.leg_a?.market_title || "")}</div>
                    <div class="venue-line">match ${num(o.match_score, 3)} · ${escapeHtml((o.venues || []).join(" / "))}</div>
                </td>
                <td>${legCell(o.leg_a)}</td>
                <td>${legCell(o.leg_b)}</td>
                <td class="num">${num(o.shares, 0)}</td>
                <td class="num">${usd(o.total_cost)}</td>
                <td class="num">${usd(o.total_fees)}</td>
                <td class="num ${signClass(o.profit)}">${usd(o.profit)}</td>
                <td class="num ${signClass(o.net_margin)}">${pct(o.net_margin)}</td>
                <td>${resolutionCell(o)}</td>
                <td>${o.tradeable
                    ? badge("tradeable", "tradeable")
                    : `${badge("blocked", "blocked")}<div class="muted small">${escapeHtml(blocked)}</div>`}</td>
            </tr>`;
        })
        .join("");
}

function legSummary(trade) {
    return (trade.legs || [])
        .map((leg) => {
            const filled = leg.filled_shares > 0
                ? `${num(leg.filled_shares, 0)} @ ${num(leg.avg_fill_price, 4)}`
                : "no fill";
            const unwound = leg.unwind_shares > 0
                ? ` · unwound ${num(leg.unwind_shares, 0)} for ${usd(leg.unwind_proceeds)}`
                : "";
            const settled = leg.settled_at ? ` · settled ${clock(leg.settled_at)}` : "";
            return `<div class="mono small">${escapeHtml(leg.venue)} ${escapeHtml(leg.side)} ${escapeHtml(leg.outcome_label)} — ${filled} [${escapeHtml(leg.status)}]${unwound}${settled}${leg.error ? ` · ${escapeHtml(leg.error)}` : ""}</div>`;
        })
        .join("");
}

function tradeFlags(trade) {
    const flags = [];
    if (trade.unmatched_leg) flags.push(badge("unmatched leg", "unverified"));
    if (trade.unwind_attempted && trade.unwind_succeeded === false) flags.push(badge("unwind failed", "exposed"));
    if (trade.containment_cost) flags.push(`<span class="muted small">containment ${usd(trade.containment_cost)}</span>`);
    if (trade.resolution_override) flags.push(badge("override", "unverified"));
    if (trade.dry_run) flags.push(badge("dry run", ""));
    if (trade.resolution_status && trade.resolution_status !== "MATCHED") {
        flags.push(badge(trade.resolution_status, trade.resolution_status.toLowerCase()));
    }
    return flags.join(" ");
}

function renderOpenTrades(trades) {
    const body = document.querySelector("#open-trades tbody");
    document.getElementById("open-count").textContent = `${trades.length} open`;

    if (!trades.length) {
        body.innerHTML = `<tr><td colspan="8" class="empty">No open positions.</td></tr>`;
        return;
    }

    body.innerHTML = trades
        .map((t) => `<tr>
            <td class="mono small">${escapeHtml(t.id)}<div class="muted">${ago(t.created_at)}</div></td>
            <td class="title-cell">${escapeHtml(t.legs?.[0]?.market_title || "")}</td>
            <td>${legSummary(t)}</td>
            <td class="num">${usd(t.actual_cost)}</td>
            <td class="num">${usd(t.expected_profit)}</td>
            <td>${badge(t.status, String(t.status).toLowerCase())}</td>
            <td>${tradeFlags(t)}</td>
            <td><button class="small danger" data-unwind="${escapeHtml(t.id)}">Unwind</button></td>
        </tr>`)
        .join("");
}

function renderVenues(data) {
    const body = document.querySelector("#venues tbody");
    const venues = data.exposure?.venues || {};
    const names = Object.keys(venues);

    if (!names.length) {
        body.innerHTML = `<tr><td colspan="6" class="empty">No venues enabled.</td></tr>`;
        return;
    }

    body.innerHTML = names
        .map((name) => {
            const venue = venues[name];
            const balance = venue.balance;
            return `<tr>
                <td>${escapeHtml(venue.label || name)}
                    ${venue.balance_known ? "" : `<div class="muted small">balance unavailable — trades on this venue are blocked</div>`}</td>
                <td class="num">${balance ? usd(balance.available) : "—"}</td>
                <td class="num">${balance ? usd(balance.total) : "—"}</td>
                <td class="num">${usd(venue.committed)}</td>
                <td class="num">${usd(venue.max_exposure)}</td>
                <td class="num">${usd(venue.headroom)}</td>
            </tr>`;
        })
        .join("");
}

/* --- trades tab ------------------------------------------------------- */

async function loadTrades() {
    const data = await api("/trades?limit=200&days=30");
    const totals = data.pnl?.totals || {};

    document.getElementById("trade-strip").innerHTML = [
        tile("Realised P&L (30d)", usd(totals.realised_pnl), `${totals.trades || 0} trades`, signClass(totals.realised_pnl)),
        tile("Expected profit", usd(totals.expected_profit), "at detection"),
        tile("Capital deployed", usd(totals.capital_deployed), ""),
        tile("Unmatched legs", totals.unmatched_legs || 0,
             `containment cost ${usd(totals.containment_cost)}`,
             (totals.unmatched_legs || 0) > 0 ? "bad" : "good"),
        tile("Failed unwinds", totals.failed_unwinds || 0, "", (totals.failed_unwinds || 0) > 0 ? "bad" : "good"),
        tile("Open / settled", `${totals.open || 0} / ${totals.settled || 0}`,
             `${totals.exposed || 0} exposed`, (totals.exposed || 0) > 0 ? "bad" : ""),
    ].join("");

    const body = document.querySelector("#trades tbody");
    document.getElementById("trades-count").textContent = `${data.count} trades`;

    if (!data.trades.length) {
        body.innerHTML = `<tr><td colspan="9" class="empty">No trades recorded.</td></tr>`;
        return;
    }

    body.innerHTML = data.trades
        .map((t) => `<tr>
            <td class="mono small">${clock(t.created_at)}</td>
            <td class="mono small">${escapeHtml(t.id)}</td>
            <td class="title-cell">${escapeHtml(t.legs?.[0]?.market_title || "")}</td>
            <td>${legSummary(t)}
                ${t.settlement_notes ? `<div class="muted small">${escapeHtml(t.settlement_notes)}</div>` : ""}</td>
            <td class="num">${usd(t.actual_cost)}</td>
            <td class="num ${signClass(t.realised_pnl)}">${t.realised_pnl === null || t.realised_pnl === undefined ? "—" : usd(t.realised_pnl)}</td>
            <td>${badge(t.status, String(t.status).toLowerCase())}</td>
            <td>${tradeFlags(t)}</td>
            <td>${["OPEN", "EXPOSED", "PENDING"].includes(t.status)
                ? `<button class="small danger" data-unwind="${escapeHtml(t.id)}">Unwind</button>` : ""}</td>
        </tr>`)
        .join("");
}

async function unwindTrade(tradeId) {
    if (!window.confirm(`Sell every filled leg of ${tradeId} at market, now?`)) return;
    try {
        const result = await api(`/trades/${tradeId}/unwind`, { method: "POST" });
        window.alert(result.ok
            ? `Unwound. Realised P&L ${usd(result.realised_pnl)}.`
            : `Unwind incomplete: ${JSON.stringify(result.legs)}`);
    } catch (error) {
        window.alert(`Unwind failed: ${error.message}`);
    }
    await refresh();
}

/* --- history tab ------------------------------------------------------ */

async function loadHistory() {
    const data = await api("/history?days=30");
    const totals = data.pnl?.totals || {};

    document.getElementById("history-strip").innerHTML = [
        tile("Scans archived", data.scan_count || 0, "in GCS"),
        tile("Resolution failures", pct(data.resolution_failure_rate),
             "of current pairs", data.resolution_failure_rate > 0.5 ? "warn" : ""),
        tile("Matched pairs", data.resolution_counts?.MATCHED || 0, "tradeable if margin allows", "good"),
        tile("Differs", data.resolution_counts?.DIFFERS || 0, "never tradeable", "bad"),
        tile("Unverified", data.resolution_counts?.UNVERIFIED || 0, "recorded, not traded", "warn"),
        tile("Realised P&L (30d)", usd(totals.realised_pnl), "", signClass(totals.realised_pnl)),
    ].join("");

    const spread = data.spread_distribution || {};
    bars(document.getElementById("spread-dist"), Object.entries(spread),
         Object.values(spread).reduce((a, b) => a + b, 0));

    const resolutionCounts = data.resolution_counts || {};
    bars(document.getElementById("resolution-dist"), Object.entries(resolutionCounts),
         Object.values(resolutionCounts).reduce((a, b) => a + b, 0));

    const perDay = data.pnl?.per_day || {};
    const dayBody = document.querySelector("#pnl-by-day tbody");
    const days = Object.keys(perDay);
    dayBody.innerHTML = days.length
        ? days.map((day) => {
            const row = perDay[day];
            return `<tr>
                <td class="mono">${escapeHtml(day)}</td>
                <td class="num">${row.trades}</td>
                <td class="num">${usd(row.capital)}</td>
                <td class="num ${signClass(row.realised_pnl)}">${usd(row.realised_pnl)}</td>
                <td class="num ${row.unmatched ? "bad" : ""}">${row.unmatched}</td>
            </tr>`;
        }).join("")
        : `<tr><td colspan="5" class="empty">No trades yet.</td></tr>`;

    const scanBody = document.querySelector("#scans tbody");
    document.getElementById("scan-count").textContent = `${data.scans?.length || 0} shown`;
    scanBody.innerHTML = (data.scans || []).length
        ? data.scans.map((scan) => `<tr>
            <td class="mono small">${clock(scan.created)}</td>
            <td class="mono small">${escapeHtml(scan.path)}</td>
            <td class="num">${((scan.size || 0) / 1024).toFixed(1)} KB</td>
        </tr>`).join("")
        : `<tr><td colspan="3" class="empty">No archived scans.</td></tr>`;
}

/* --- orchestration ---------------------------------------------------- */

async function refresh() {
    try {
        if (currentTab === "dashboard") {
            const data = await api("/dashboard");
            lastDashboard = data;
            renderKillBar(data.status || {});
            renderAlerts(data.alerts);
            renderStatusStrip(data);
            renderOpportunities(data.opportunities || []);
            renderOpenTrades(data.open_trades || []);
            renderVenues(data);
        } else if (currentTab === "trades") {
            await loadTrades();
        } else if (currentTab === "history") {
            await loadHistory();
        }
        document.getElementById("footer-note").textContent =
            `Updated ${new Date().toLocaleTimeString()} · self-hosted pmxt · execution requires resolution_status MATCHED`;
    } catch (error) {
        document.getElementById("footer-note").textContent = `Refresh failed: ${error.message}`;
    }
}

function selectTab(tab) {
    currentTab = tab;
    document.querySelectorAll("nav a[data-tab]").forEach((link) => {
        link.classList.toggle("active", link.dataset.tab === tab);
    });
    document.querySelectorAll(".tab-panel").forEach((panel) => {
        panel.hidden = panel.id !== `tab-${tab}`;
    });
    refresh();
}

function startRefresh() {
    if (refreshTimer) clearInterval(refreshTimer);
    refreshTimer = setInterval(() => {
        if (!document.hidden) refresh();
    }, REFRESH_MS);
}

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll("nav a[data-tab]").forEach((link) => {
        link.addEventListener("click", (event) => {
            event.preventDefault();
            selectTab(link.dataset.tab);
        });
    });

    document.getElementById("kill-button").addEventListener("click", toggleKill);

    document.getElementById("scan-now").addEventListener("click", async (event) => {
        const button = event.currentTarget;
        button.disabled = true;
        button.textContent = "Scanning…";
        try {
            const result = await api("/scan", { method: "POST" });
            window.alert(
                `Scan ${result.scan_id}\n` +
                `${result.markets_total} markets, ${result.pairs_considered} pairs, ` +
                `${result.opportunities_found} opportunities (${result.opportunities_tradeable} tradeable)\n` +
                `${result.trades_placed} trade(s) placed in ${result.duration_seconds}s`
            );
        } catch (error) {
            window.alert(`Scan failed: ${error.message}`);
        } finally {
            button.disabled = false;
            button.textContent = "Scan now";
            refresh();
        }
    });

    document.addEventListener("click", (event) => {
        const tradeId = event.target?.dataset?.unwind;
        if (tradeId) unwindTrade(tradeId);
    });

    document.addEventListener("visibilitychange", () => {
        if (!document.hidden) refresh();
    });

    refresh();
    startRefresh();
});
