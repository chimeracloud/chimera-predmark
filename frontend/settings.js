/* Chimera PredMark — settings.
 *
 * Two quite different things happen on this page.
 *
 * Configuration is read from and written to Firestore via GET/PUT /settings.
 * Only changed fields are sent, and the backend records a field-level audit
 * entry for each one.
 *
 * Credentials never touch that path. They are POSTed one at a time to
 * PUT /settings/credentials, which writes to Secret Manager and returns a
 * masked confirmation. Nothing on this page ever holds a stored value: the
 * inputs start empty every time, are cleared immediately after a successful
 * write, and there is no endpoint that could populate them.
 */

const API_BASE = (window.PREDMARK_API || "/api/predmark").replace(/\/$/, "");

let loaded = null; // the settings document as last read from the server

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
        throw new Error(detail);
    }
    return response.json();
}

const escapeHtml = (value) =>
    String(value ?? "").replace(/[&<>"']/g, (c) => ({
        "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    })[c]);

function getPath(object, path) {
    return path.split(".").reduce((node, key) => (node == null ? undefined : node[key]), object);
}

function setPath(object, path, value) {
    const keys = path.split(".");
    const last = keys.pop();
    const target = keys.reduce((node, key) => (node[key] = node[key] || {}), object);
    target[last] = value;
}

function status(message, kind) {
    const element = document.getElementById("save-status");
    element.textContent = message;
    element.className = `status ${kind || ""}`;
}

/* --- configuration form ----------------------------------------------- */

function fillForm(settings) {
    document.querySelectorAll("input[id], select[id]").forEach((input) => {
        if (!input.id.includes(".") || input.dataset.credential) return;
        const value = getPath(settings, input.id);
        if (value === undefined) return;

        if (input.type === "checkbox") {
            input.checked = Boolean(value);
        } else if (input.dataset.list === "true") {
            input.value = Array.isArray(value) ? value.join(", ") : "";
        } else {
            // A null optional setting means "fall back to the global value",
            // and must render as an empty box rather than the text "null".
            input.value = value === null ? "" : value;
        }
    });
    updateOverrideWarning();
}

function readForm() {
    const patch = {};
    document.querySelectorAll("input[id], select[id]").forEach((input) => {
        if (!input.id.includes(".") || input.dataset.credential) return;

        let value;
        if (input.type === "checkbox") {
            value = input.checked;
        } else if (input.dataset.list === "true") {
            value = input.value.split(",").map((s) => s.trim()).filter(Boolean);
        } else if (input.type === "number") {
            if (input.value === "") {
                // Nullable settings must be clearable back to "use the
                // global fallback". Non-nullable ones are simply omitted, so
                // an empty box never blanks a required number.
                if (input.dataset.nullable !== "true") return;
                value = null;
            } else {
                value = Number(input.value);
                if (Number.isNaN(value)) return;
            }
        } else {
            value = input.value;
        }
        setPath(patch, input.id, value);
    });
    return patch;
}

function renderVenues(settings, catalogue) {
    const container = document.getElementById("venue-settings");
    container.innerHTML = Object.entries(settings.venues || {})
        .map(([name, venue]) => {
            const spec = (catalogue || []).find((v) => v.name === name) || {};
            return `<div class="venue-block">
                <div class="venue-head">
                    <input type="checkbox" id="venues.${name}.enabled">
                    <strong>${escapeHtml(venue.label || name)}</strong>
                    <span class="muted small">${escapeHtml(spec.notes || "")}</span>
                </div>
                <div class="form-grid">
                    <div class="field">
                        <label for="venues.${name}.fee_model.model">Fee model</label>
                        <select id="venues.${name}.fee_model.model">
                            <option value="none">None</option>
                            <option value="flat_bps">Flat basis points</option>
                            <option value="kalshi_quadratic">Kalshi quadratic</option>
                        </select>
                        <div class="help">Kalshi's fee peaks at 50c, where most arbitrage sits. Confirm against the venue's published schedule before funding.</div>
                    </div>
                    <div class="field">
                        <label for="venues.${name}.fee_model.taker_bps">Taker fee (bps)</label>
                        <input type="number" id="venues.${name}.fee_model.taker_bps" min="0" step="1">
                    </div>
                    <div class="field">
                        <label for="venues.${name}.fee_model.maker_bps">Maker fee (bps)</label>
                        <input type="number" id="venues.${name}.fee_model.maker_bps" min="0" step="1">
                    </div>
                    <div class="field">
                        <label for="venues.${name}.fee_model.quadratic_rate">Quadratic rate</label>
                        <input type="number" id="venues.${name}.fee_model.quadratic_rate" min="0" max="1" step="0.005">
                        <div class="help">Kalshi publishes 0.07 generally, 0.035 for selected series.</div>
                    </div>
                    <div class="field">
                        <label for="venues.${name}.fee_model.fixed_cost_per_order">Fixed cost per order (USD)</label>
                        <input type="number" id="venues.${name}.fee_model.fixed_cost_per_order" min="0" step="0.01">
                    </div>
                    <div class="field">
                        <label for="venues.${name}.poll_priority">Poll priority</label>
                        <input type="number" id="venues.${name}.poll_priority" min="1" step="1">
                    </div>
                    <div class="field">
                        <label for="venues.${name}.market_limit">Markets per scan</label>
                        <input type="number" id="venues.${name}.market_limit" min="1" step="10">
                    </div>
                    <div class="field">
                        <label for="venues.${name}.max_exposure">Max exposure (USD)</label>
                        <input type="number" id="venues.${name}.max_exposure" min="0" step="10">
                    </div>
                    <div class="field">
                        <label for="venues.${name}.min_liquidity">Min liquidity (USD)</label>
                        <input type="number" data-nullable="true" id="venues.${name}.min_liquidity" min="0" step="100">
                        <div class="help">Blank falls back to the global scanning floor. Kalshi reports no liquidity at all, so its floor belongs on volume instead.</div>
                    </div>
                    <div class="field">
                        <label for="venues.${name}.min_volume_24h">Min 24h volume (USD)</label>
                        <input type="number" data-nullable="true" id="venues.${name}.min_volume_24h" min="0" step="50">
                        <div class="help">Blank falls back to the global scanning floor.</div>
                    </div>
                    <div class="field">
                        <label for="venues.${name}.order_type">Order type</label>
                        <select id="venues.${name}.order_type">
                            <option value="market">Market</option>
                            <option value="limit">Limit</option>
                        </select>
                        <div class="help">Market takes liquidity immediately. A resting limit order is the main way a hedged trade becomes an unhedged one.</div>
                    </div>
                    <div class="field">
                        <label for="venues.${name}.slippage_pct">Slippage tolerance (%)</label>
                        <input type="number" id="venues.${name}.slippage_pct" min="0" max="100" step="0.5">
                    </div>
                </div>
            </div>`;
        })
        .join("");
}

function updateOverrideWarning() {
    const allow = document.getElementById("risk.allow_unverified_override");
    const required = document.getElementById("risk.required_resolution_status");
    const warning = document.getElementById("override-warning");
    warning.hidden = !(allow?.checked && required?.value === "MATCHED_OR_UNVERIFIED");
}

/* --- credentials ------------------------------------------------------ */

function renderCredentials(catalogue, credentials) {
    const container = document.getElementById("credential-blocks");
    container.innerHTML = (catalogue || [])
        .map((venue) => {
            const state = credentials[venue.name] || [];
            const rows = venue.credentials
                .map((field) => {
                    const current = state.find((c) => c.key === field.key) || {};
                    const input = field.multiline
                        ? `<textarea data-credential="true" data-venue="${venue.name}" data-key="${field.key}" placeholder="paste the PEM block, including BEGIN and END lines"></textarea>`
                        : `<input type="password" data-credential="true" data-venue="${venue.name}" data-key="${field.key}" placeholder="enter to set or replace" autocomplete="new-password">`;
                    return `<div class="cred-row">
                        <div class="cred-label">${escapeHtml(field.label)}
                            ${field.required_for_trading ? "" : `<div class="muted small">optional</div>`}
                            ${field.help ? `<div class="muted small">${escapeHtml(field.help)}</div>` : ""}
                        </div>
                        <div>${input}
                            <div class="muted small">secret: ${escapeHtml(field.secret_id)}</div>
                        </div>
                        <div class="cred-state ${current.configured ? "configured" : "missing"}">
                            ${current.configured ? `configured ${escapeHtml(current.masked || "")}` : "not configured"}
                            ${current.error ? `<div class="muted small">${escapeHtml(current.error)}</div>` : ""}
                        </div>
                        <div><button class="small" data-save-credential="${venue.name}:${field.key}">Store</button></div>
                    </div>`;
                })
                .join("");

            return `<div class="venue-block">
                <div class="venue-head">
                    <strong>${escapeHtml(venue.label)}</strong>
                    <span class="muted small">${escapeHtml(venue.notes || "")}</span>
                </div>
                ${rows}
            </div>`;
        })
        .join("");
}

async function storeCredential(venue, key) {
    const input = document.querySelector(
        `[data-credential="true"][data-venue="${venue}"][data-key="${key}"]`
    );
    if (!input || !input.value.trim()) {
        status(`Nothing entered for ${venue} ${key}`, "err");
        return;
    }

    status(`Storing ${venue} ${key}…`);
    try {
        const result = await api("/settings/credentials", {
            method: "PUT",
            body: JSON.stringify({ venue, key, value: input.value.trim() }),
        });
        // Clear immediately: the value has no business remaining in the DOM.
        input.value = "";
        status(`Stored ${venue} ${key} (${result.masked}) as version ${result.version}`, "ok");
        await load();
        document.querySelector('.tabs button[data-panel="credentials"]').click();
    } catch (error) {
        status(`Failed to store ${venue} ${key}: ${error.message}`, "err");
    }
}

/* --- audit ------------------------------------------------------------ */

async function loadAudit() {
    const data = await api("/settings/audit?limit=200");
    const body = document.querySelector("#audit tbody");
    const rows = [];

    for (const entry of data.audit || []) {
        for (const change of entry.changes || []) {
            rows.push(`<tr>
                <td class="mono small">${escapeHtml(new Date(entry.at).toLocaleString())}</td>
                <td class="mono small">${escapeHtml(entry.actor || "")}</td>
                <td class="mono small">${escapeHtml(change.field)}</td>
                <td class="mono small">${escapeHtml(JSON.stringify(change.from))}</td>
                <td class="mono small">${escapeHtml(JSON.stringify(change.to))}</td>
                <td class="muted small">${escapeHtml(entry.note || "")}</td>
            </tr>`);
        }
    }

    document.getElementById("audit-count").textContent = `${rows.length} changes`;
    body.innerHTML = rows.length
        ? rows.join("")
        : `<tr><td colspan="6" class="empty">No settings changes recorded.</td></tr>`;
}

/* --- orchestration ---------------------------------------------------- */

async function load() {
    status("Loading…");
    try {
        const data = await api("/settings");
        loaded = data.settings;
        renderVenues(data.settings, data.venue_catalogue);
        fillForm(data.settings);
        renderCredentials(data.venue_catalogue, data.credentials || {});
        status(
            `Loaded. Last changed ${data.settings.updated_at ? new Date(data.settings.updated_at).toLocaleString() : "never"}` +
            `${data.settings.updated_by ? ` by ${data.settings.updated_by}` : ""}.`,
            "ok"
        );
    } catch (error) {
        status(`Load failed: ${error.message}`, "err");
    }
}

async function save() {
    const button = document.getElementById("save");
    button.disabled = true;
    status("Saving…");
    try {
        const result = await api("/settings", {
            method: "PUT",
            body: JSON.stringify(readForm()),
        });
        loaded = result.settings;
        fillForm(result.settings);
        const changed = result.changes?.length || 0;
        status(
            changed
                ? `Saved. ${changed} field(s) changed: ${result.changes.map((c) => c.field).join(", ")}`
                : "Saved. No changes.",
            "ok"
        );
    } catch (error) {
        status(`Save failed: ${error.message}`, "err");
    } finally {
        button.disabled = false;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    document.querySelectorAll(".tabs button").forEach((button) => {
        button.addEventListener("click", () => {
            document.querySelectorAll(".tabs button").forEach((b) => b.classList.remove("active"));
            button.classList.add("active");
            document.querySelectorAll(".panel").forEach((panel) => {
                panel.hidden = panel.id !== `panel-${button.dataset.panel}`;
            });
            if (button.dataset.panel === "audit") loadAudit();
        });
    });

    document.getElementById("save").addEventListener("click", save);
    document.getElementById("reload").addEventListener("click", load);

    document.addEventListener("click", (event) => {
        const target = event.target?.dataset?.saveCredential;
        if (target) {
            const [venue, key] = target.split(":");
            storeCredential(venue, key);
        }
    });

    document.addEventListener("change", (event) => {
        if (event.target.id?.startsWith("risk.")) updateOverrideWarning();
    });

    load();
});
