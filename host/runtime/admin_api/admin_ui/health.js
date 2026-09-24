// Host health plus the always-visible top-bar runtime status and usage.

import { api } from "./api.js";
import {
  $, badge, bedrockUsage, clampPercent, costUnits, esc, formatCost, formatTokenCount, gib, inlineMessage,
  notice, setHtml, RUNTIME_PROVIDERS,
} from "./helpers.js";
import { renderIntegrationAccounts, setBedrockCredentialMetadata } from "./network.js";

let latestRuntimes = [];
let latestAccounts = [];
let latestHostInferenceProviders = [];
let latestToolUsage = null;
let toolUsageStale = false;

async function loadToolUsage() {
  try {
    latestToolUsage = await api("GET", "/v1/tools/usage");
    toolUsageStale = false;
  } catch (_error) { toolUsageStale = true; }
}

export function providerAccounts() {
  return latestAccounts;
}

export function runtimeRecords() {
  return latestRuntimes;
}

function statTile(label, valueHtml, extraClass = "") {
  return `<div class="stat-tile${extraClass ? " " + extraClass : ""}"><div class="stat-label">${esc(label)}</div><div class="stat-value">${valueHtml}</div></div>`;
}

function meterTile(label, valueHtml, percent) {
  const clamped = Math.max(0, Math.min(100, percent));
  return `
    <div class="stat-tile">
      <div class="stat-label">${esc(label)}</div>
      <div class="stat-value stat-meter-value">${valueHtml}</div>
      <progress class="meter${clamped >= 80 ? " hot" : ""}" max="100" value="${clamped.toFixed(1)}"></progress>
    </div>`;
}

function usageTile(label, used, total, totalLabel) {
  const unit = totalLabel || "GiB";
  const value = `<span class="metric-main">${esc(gib(used))} ${esc(unit)}</span><span class="metric-total">of ${esc(gib(total))} ${esc(unit)}</span>`;
  return meterTile(label, value, total > 0 ? (used / total) * 100 : 0);
}

function memorySwapTile(memory, swap) {
  const memoryUsed = Number(memory?.used_bytes) || 0;
  const memoryTotal = Number(memory?.total_bytes) || 0;
  const swapUsed = Number(swap?.used_bytes) || 0;
  const swapTotal = Number(swap?.allocated_bytes) || 0;
  const combinedTotal = memoryTotal + swapTotal;
  const usedPercent = combinedTotal > 0 ? ((memoryUsed + swapUsed) / combinedTotal) * 100 : 0;
  const clamped = clampPercent(usedPercent);
  return `
    <div class="stat-tile memory-swap-tile">
      <div class="stat-label">Memory</div>
      <div class="memory-swap-values">
        <div>
          <span class="metric-main">${esc(gib(memoryUsed))} GiB</span>
          <span class="metric-total">memory of ${esc(gib(memoryTotal))} GiB</span>
        </div>
        <div>
          <span class="metric-main">${esc(gib(swapUsed))} GiB</span>
          <span class="metric-total">swap of ${esc(gib(swapTotal))} GiB</span>
        </div>
      </div>
      <progress class="meter${Number(clamped) >= 80 ? " hot" : ""}" max="100" value="${esc(clamped)}"></progress>
    </div>`;
}

function filesystemMountTile(label, mount) {
  if (!mount || typeof mount !== "object") {
    return statTile(label, `<span class="muted">not mounted</span>`);
  }
  return usageTile(label, mount.used_bytes, mount.total_bytes);
}

function historyStat(label, value, description) {
  const count = Number.isInteger(value) && value >= 0 ? value : 0;
  const formatted = new Intl.NumberFormat().format(count);
  const accessible = `${label}: ${formatted}. ${description}`;
  return `
    <div class="history-stat" aria-label="${esc(accessible)}" title="${esc(description)}">
      <span class="history-stat-value">${esc(formatted)}</span>
      <span class="history-stat-label">${esc(label)}</span>
    </div>`;
}

function renderHealthIssues(issues) {
  if (!Array.isArray(issues) || issues.length === 0) return "";
  const rows = issues.map(issue => `
    <li class="health-issue">
      <div class="health-issue-summary">${esc(issue?.summary || "Host health issue")}</div>
      <div class="health-issue-detail">${esc(issue?.detail || "No additional detail was reported.")}</div>
      <div class="health-issue-next"><strong>Next:</strong> ${esc(issue?.next_step || "Review the affected host component.")}</div>
    </li>`).join("");
  return `
    <section class="health-issues" aria-label="Why host health is degraded">
      <div class="health-issues-title">Needs attention</div>
      <ul>${rows}</ul>
    </section>`;
}

export async function refreshHealth() {
  const health = await api("GET", "/v1/health");
  $("agent-name").textContent = health.agent_name ? `Host: ${health.agent_name}` : "";
  $("agent-name").hidden = !health.agent_name;
  const runtimes = Array.isArray(health.agent_runtime.runtimes) ? health.agent_runtime.runtimes : [];
  latestRuntimes = runtimes;
  const host = health.host_runtime;
  const mounts = host.filesystem?.mounts || {};
  const history = health.history || {};
  const tokens = health.lifetime_tokens || {};
  setHtml($("health"), `
    ${renderHomeUpgrade(health.upgrade)}
    <div class="stat-grid stat-statuses">
      ${statTile("Overall", badge(health.status))}
      ${statTile("Network controls", badge(health.network_controls.status))}
      ${statTile("Version", renderVersion(health.version), "stat-wide version-tile")}
    </div>
    ${renderHealthIssues(health.issues)}
    <div class="stat-grid stat-meters">
      ${meterTile("CPU", `<span class="metric-main">${esc(host.cpu.usage_percent)}%</span>`, Number(host.cpu.usage_percent) || 0)}
      ${memorySwapTile(host.memory, host.swap)}
      ${filesystemMountTile("Root volume", mounts.root)}
      ${filesystemMountTile("Admin volume", mounts.admin)}
      ${filesystemMountTile("Agent volume", mounts.agent)}
    </div>
    <div class="stat-history" aria-label="Agent stats">
      <div class="stat-history-title">Stats</div>
      <div class="stat-history-grid">
        ${historyStat("Threads", history.threads, "All agent threads recorded on this host.")}
        ${historyStat("Inbound messages", history.messages, "Messages sent to agents on this host.")}
        ${historyStat("Agent activity", history.activities, "Agent messages, tool calls, commands, reasoning, and other recorded agent work.")}
      </div>
      <div class="lifetime-token-title">Lifetime tokens · All providers</div>
      <div class="stat-history-grid lifetime-tokens" aria-label="Lifetime token usage">
        ${historyStat("Input tokens", (tokens.input_tokens ?? 0) + (tokens.cached_input_tokens ?? 0) + (tokens.cache_write_tokens ?? 0), "Known total input tokens since usage tracking began, including cached input and cache writes.")}
        ${historyStat("Of which cached", tokens.cached_input_tokens, "Cached input tokens already included in the input total.")}
        ${historyStat("Output tokens", tokens.output_tokens, "Known generated tokens since usage tracking began, including reported reasoning.")}
      </div>
    </div>`);
  renderRuntimeOverview();
  renderIntegrationAccounts();
  // Restore pending codes only on the visible provider card.
  for (const runtime of runtimes) {
    if (runtime.status === "awaiting_login") await showOauth(false, runtime.type);
    else oauthRetryAt.delete(runtime.type);
  }
}

function renderVersion(version) {
  if (!version || typeof version !== "object") return `<span class="muted">not reported</span>`;
  const status = typeof version.status === "string" && version.status ? version.status : "unknown";
  const runtime = typeof version.runtime === "string" && version.runtime ? version.runtime : "unknown";
  const mismatch = status === "ok" ? "" : `${badge(status)} `;
  return `<span class="version-runtime">${mismatch}${esc(runtime)}</span>`;
}

function renderHomeUpgrade(upgrade) {
  const available = upgrade?.available === true && typeof upgrade.latest === "string";
  if (!available) return "";
  return `<p class="home-upgrade-notice"><strong>Upgrade available: version ${esc(upgrade.latest)}</strong><span>Use your operator plane to upgrade.</span></p>`;
}

export async function refreshProviderAccounts() {
  const [response, bedrockCredentials, hostInference] = await Promise.all([
    api("GET", "/v1/agent-runtime/account"),
    api("GET", "/v1/agent-runtime/bedrock-credentials"),
    api("GET", "/v1/host-inference/providers").catch(() => null),
    loadToolUsage(),
  ]);
  setBedrockCredentialMetadata(bedrockCredentials);
  if (Array.isArray(hostInference?.providers)) latestHostInferenceProviders = hostInference.providers;
  renderProviderAccounts(response);
}

function renderProviderAccounts(response) {
  latestAccounts = Array.isArray(response.accounts) ? response.accounts : [];
  renderRuntimeOverview();
  renderIntegrationAccounts();
}

let pendingRuntimeAccountRefresh = null;

function refreshRuntimeAccounts() {
  if (!pendingRuntimeAccountRefresh) {
    pendingRuntimeAccountRefresh = api("POST", "/v1/agent-runtime/refresh", {})
      .finally(() => { pendingRuntimeAccountRefresh = null; });
  }
  return pendingRuntimeAccountRefresh;
}

export async function refreshProviderUsage() {
  const [response, hostInference] = await Promise.all([
    refreshRuntimeAccounts(),
    api("GET", "/v1/host-inference/providers").catch(() => null),
    loadToolUsage(),
  ]);
  if (Array.isArray(hostInference?.providers)) latestHostInferenceProviders = hostInference.providers;
  renderProviderAccounts(response);
  await refreshHealth();
}

// The top bar exposes provider families and tool spend as compact menus. One
// shared value makes them mutually exclusive and survives the 5-second render.
let expandedOverviewGroup = null;

export function toggleRuntimeOverview(group) {
  if (!["runtimes", "host-ai", "tools"].includes(group)) return;
  expandedOverviewGroup = expandedOverviewGroup === group ? null : group;
  applyOverviewExpanded();
  // Opening runs the existing hard provider refresh. There is no second refresh
  // control inside the menu, so desktop and phone use the same interaction.
  if (expandedOverviewGroup) refreshProviderUsage().catch(() => {});
}

export function collapseRuntimeOverview() {
  if (!expandedOverviewGroup) return;
  expandedOverviewGroup = null;
  applyOverviewExpanded();
}

function applyOverviewExpanded() {
  const container = $("runtime-overview");
  if (!container) return;
  for (const group of container.querySelectorAll(".runtime-overview-group")) {
    const expanded = group.dataset.overviewGroup === expandedOverviewGroup;
    group.classList.toggle("expanded", expanded);
    const toggle = group.querySelector(".runtime-overview-toggle");
    if (toggle) toggle.setAttribute("aria-expanded", String(expanded));
  }
}

function runtimeRunningCount() {
  return latestRuntimes.reduce(
    (total, runtime) => total + (Array.isArray(runtime.active_thread_ids) ? runtime.active_thread_ids.length : 0),
    0,
  );
}

function renderRuntimeOverview() {
  const container = $("runtime-overview");
  if (!container) return;
  const bedrockAccount = latestAccounts.find(entry => entry.provider === "bedrock") || {};
  const runtimeBoxes = [
    subscriptionSummary("codex"),
    subscriptionSummary("codex-2"),
    subscriptionSummary("codex-3"),
    subscriptionSummary("claude_code"),
    grokSummary("grok"),
    grokSummary("grok-2"),
    bedrockSummary("hermes", bedrockAccount),
  ].join("");
  const hostAiBoxes = [
    hostInferenceSummary("openai", "OpenAI host", "host_openai"),
    hostInferenceSummary("typesafe", "TypeSafe", "host_typesafe"),
  ].join("");
  const running = runtimeRunningCount();
  const summaryText = running ? `${running} running` : "All idle";
  const summaryLabel = running
    ? `${running} agent turn${running === 1 ? "" : "s"} running`
    : "All agent runtimes idle";
  const hostAi = hostInferenceGroupSummary();
  const toolSpend = latestToolUsage ? `${formatCost(latestToolUsage.month_to_date)} MTD` : "Unavailable";
  const toolStatus = toolUsageStale ? " · stale" : "";
  setHtml(container, `
    <div class="runtime-overview-group" data-overview-group="runtimes">
      <button class="runtime-overview-toggle" data-action="toggle-runtime-overview" data-overview-group="runtimes" aria-expanded="${expandedOverviewGroup === "runtimes"}" aria-controls="runtime-overview-runtimes-panel" aria-label="Agent runtimes: ${esc(summaryLabel)}. Show provider status and usage">
        <span class="rot-icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 20 20"><path d="M3.5 14.5a6.5 6.5 0 1 1 13 0" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/><path d="M10 14.5 13 8.6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg></span>
        <span class="rot-copy"><span class="rot-label">Agent runtimes</span><span class="rot-summary${running ? " busy" : ""}">${esc(summaryText)}</span></span>
        <span class="rot-chevron" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 20 20"><path d="m5.5 8 4.5 4.5L14.5 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
      </button>
      <div class="runtime-overview-panel" id="runtime-overview-runtimes-panel">
        ${runtimeBoxes}
      </div>
    </div>
    <div class="runtime-overview-group" data-overview-group="host-ai">
      <button class="runtime-overview-toggle" data-action="toggle-runtime-overview" data-overview-group="host-ai" aria-expanded="${expandedOverviewGroup === "host-ai"}" aria-controls="runtime-overview-host-ai-panel" aria-label="Host AI: ${esc(hostAi.label)}. Show provider status and usage">
        <span class="rot-icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 20 20"><path d="M10 2.8v2.4M10 14.8v2.4M2.8 10h2.4M14.8 10h2.4M5 5l1.7 1.7M13.3 13.3 15 15M15 5l-1.7 1.7M6.7 13.3 5 15" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/><circle cx="10" cy="10" r="2.7" fill="none" stroke="currentColor" stroke-width="1.5"/></svg></span>
        <span class="rot-copy"><span class="rot-label">Host AI</span><span class="rot-summary">${esc(hostAi.text)}</span></span>
        <span class="rot-chevron" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 20 20"><path d="m5.5 8 4.5 4.5L14.5 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
      </button>
      <div class="runtime-overview-panel" id="runtime-overview-host-ai-panel">
        ${hostAiBoxes}
      </div>
    </div>
    <div class="runtime-overview-group" data-overview-group="tools">
      <button class="runtime-overview-toggle" data-action="toggle-runtime-overview" data-overview-group="tools" aria-expanded="${expandedOverviewGroup === "tools"}" aria-controls="runtime-overview-tools-panel" aria-label="Tools: ${esc(toolSpend + toolStatus)}. Show reported tool spend">
        <span class="rot-icon" aria-hidden="true"><svg width="16" height="16" viewBox="0 0 20 20"><path d="m10 2 7 4v8l-7 4-7-4V6l7-4Zm-7 4 7 4 7-4M10 10v8" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg></span>
        <span class="rot-copy"><span class="rot-label">Tools</span><span class="rot-summary">${esc(toolSpend)}</span></span>
        <span class="rot-chevron" aria-hidden="true"><svg width="14" height="14" viewBox="0 0 20 20"><path d="m5.5 8 4.5 4.5L14.5 8" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
      </button>
      <div class="runtime-overview-panel" id="runtime-overview-tools-panel">${toolUsagePanel()}</div>
    </div>`);
  applyOverviewExpanded();
}

// The shared top-bar box for one runtime: a status dot, the runtime label and
// its live process status, a usage readout on the right, and a running-turn
// corner badge. `usageHtml` and `usageSummaryText` are the only things that
// differ between a subscription runtime (quota rings) and a Bedrock runtime (a
// month-to-date cost estimate).
function runtimeSummaryCard(runtime, usageHtml, usageSummaryText, extraClass) {
  const meta = RUNTIME_PROVIDERS[runtime];
  const record = latestRuntimes.find(entry => entry.type === runtime) || { status: "loading" };
  const running = Array.isArray(record.active_thread_ids) ? record.active_thread_ids.length : 0;
  const statusText = String(record.status || "loading").replaceAll("_", " ");
  const runningLabel = running ? `; ${running} running` : "";
  // The running count is a corner badge rather than inline text so a long
  // status ("awaiting login") never truncates it away.
  const runningBadge = running
    ? `<span class="runtime-running-badge" aria-hidden="true">${running} running</span>` : "";
  const usage = usageHtml
    ? `<span class="runtime-usage">${usageHtml}</span>`
    : "";
  const inner = `
        <span class="runtime-summary-name">
          <span class="runtime-status-dot ${esc(record.status)}" aria-hidden="true"></span>
          <span class="runtime-summary-copy">
            <span>${esc(meta.label)}</span>
            <span class="runtime-state">${esc(statusText)}</span>
          </span>
        </span>
        ${usage}
        ${runningBadge}`;
  const cls = `runtime-summary${extraClass ? ` ${extraClass}` : ""}`;
  // Every box links to its focused Home integration page, in
  // any state — to connect or re-enable a deactivated runtime, or to manage
  // credentials and integration settings for an active one.
  const summaryLabel = `${meta.label}: ${statusText}${runningLabel}; ${usageSummaryText}. Open provider settings`;
  return `
      <button class="${cls}" data-action="open-provider" data-provider="${esc(meta.provider)}" data-runtime="${esc(runtime)}" aria-label="${esc(summaryLabel)}">${inner}</button>`;
}

// A subscription runtime (Codex, Claude Code): usage is quota windows, drawn as
// percentage rings.
function subscriptionSummary(runtime) {
  const account = latestAccounts.find(entry => entry.agent_runtime === runtime) || {};
  const windows = usageWindows(account);
  const modelSummary = windows.fableWeekly
    ? `; ${usageSummary(`${windows.fableWeekly.label} weekly`, windows.fableWeekly)}` : "";
  const usageHtml = [
    usageRing("5h", windows.fiveHour),
    usageRing("wk", windows.weekly),
    windows.fableWeekly ? usageRing(windows.fableWeekly.label, windows.fableWeekly) : "",
  ].join("");
  const usageSummaryText = `${usageSummary("5 hour", windows.fiveHour)}; ${usageSummary("weekly", windows.weekly)}${modelSummary}`;
  return runtimeSummaryCard(runtime, usageHtml, usageSummaryText);
}

// Grok draws one ring, not two: its subscription has a single billing period
// rather than a rolling short window plus a weekly one. The percentage is
// frequently absent because xAI omits it on a unified-billing subscription;
// in that case the runtime status stands alone rather than showing a placeholder.
function grokSummary(runtime) {
  const account = latestAccounts.find(entry => entry.agent_runtime === runtime) || {};
  const usage = account.grok_usage || {};
  const periods = {
    daily: { label: "day", summary: "daily" },
    weekly: { label: "wk", summary: "weekly" },
    monthly: { label: "mo", summary: "monthly" },
  };
  const period = periods[usage.period_type] || periods.weekly;
  const window = { usedPercent: usage.usage_percent, resetsAt: usage.resets_at };
  if (window.usedPercent === undefined || window.usedPercent === null) {
    return runtimeSummaryCard(runtime, "", UNAVAILABLE_USAGE_TEXT);
  }
  return runtimeSummaryCard(runtime, usageRing(period.label, window), usageSummary(period.summary, window));
}

const UNAVAILABLE_USAGE_TEXT = "usage monitoring is not available for Grok";

// Hermes usage is pay-per-token, so the readout is a
// month-to-date cost estimate with token totals, not a quota ring. The numbers
// come from the provider account and the box is driven by Hermes's status.
function bedrockSummary(runtime, account) {
  const usage = bedrockUsage(account);
  const usageHtml = bedrockUsageReadout(usage);
  let usageSummaryText = "no metered usage yet";
  if (usage) {
    const tokens = `${formatTokenCount(usage.inputTokens)} input tokens (of which cached: ${formatTokenCount(usage.cacheReadTokens)}) / ${formatTokenCount(usage.outputTokens)} output tokens`;
    // Surface the metered gap, so a screen reader hears why the estimate may
    // lag actual spend.
    const metered = usage.requests > usage.meteredRequests
      ? `; ${usage.meteredRequests} of ${usage.requests} requests metered` : "";
    usageSummaryText = `estimated month-to-date ${usage.cost} (${tokens})${metered}`;
  }
  return runtimeSummaryCard(runtime, usageHtml, usageSummaryText, "runtime-summary-bedrock");
}

function hostInferenceUsageReadout(usage) {
  const value = usage ? usage.cost : "--";
  return `<span class="runtime-stat runtime-stat-cost">
      <span class="runtime-stat-value">${esc(value)}</span>
      <span class="runtime-stat-label">MTD est.</span>
    </span>`;
}

// The right-hand readout for a Bedrock box: three stacked figures — input
// tokens, output tokens, and the cost — mirroring the subscription boxes' row
// of usage rings so all three boxes read at the same visual weight. The cost is
// labelled "MTD est." to flag that it is a metered estimate, not the AWS bill.
// Before any usage has been metered, the runtime status stands alone rather
// than showing placeholder values.
function bedrockUsageReadout(usage) {
  const stat = (value, label, extraClass = "") =>
    `<span class="runtime-stat${extraClass ? ` ${extraClass}` : ""}">
          <span class="runtime-stat-value">${esc(value)}</span>
          <span class="runtime-stat-label">${esc(label)}</span>
        </span>`;
  if (!usage) return "";
  return `${stat(formatTokenCount(usage.inputTokens), "in")}${stat(formatTokenCount(usage.outputTokens), "out")}${stat(usage.cost, "MTD est.", "runtime-stat-cost")}`;
}

function hostInferenceUsage(provider) {
  const raw = provider?.usage;
  const amount = raw && typeof raw === "object" ? Number(raw.month_to_date) : NaN;
  if (!Number.isFinite(amount)) return null;
  const currency = !raw.currency || raw.currency === "USD" ? "$" : `${raw.currency} `;
  const cost = formatCost(amount, currency);
  return {
    cost,
    inputTokens: Number(raw.input_tokens) || 0,
    outputTokens: Number(raw.output_tokens) || 0,
    cachedInputTokens: Number(raw.cached_input_tokens) || 0,
    requests: Number(raw.requests) || 0,
    measuredRequests: Number(raw.measured_requests) || 0,
    pricedRequests: Number(raw.priced_requests) || 0,
  };
}

function hostInferenceGroupSummary() {
  const amounts = latestHostInferenceProviders
    .map(provider => Number(provider?.usage?.month_to_date))
    .filter(Number.isFinite);
  if (!amounts.length) return { text: "No usage", label: "No metered usage yet" };
  const total = amounts.reduce((sum, amount) => sum + amount, 0);
  const text = `${formatCost(total)} MTD`;
  return { text, label: `Estimated month-to-date ${text}` };
}

function hostInferenceSummary(providerName, label, guideId) {
  const provider = latestHostInferenceProviders.find(entry => entry.provider === providerName) || {};
  const missingKey = provider.enabled && !provider.configured;
  const status = missingKey ? "API key not set"
    : provider.enabled ? "active" : provider.configured ? "configured" : "disabled";
  const usage = hostInferenceUsage(provider);
  const usageHtml = hostInferenceUsageReadout(usage);
  let usageSummaryText = "no metered usage yet";
  if (usage) {
    const cached = usage.cachedInputTokens
      ? `, including ${formatTokenCount(usage.cachedInputTokens)} cached` : "";
    const measured = usage.requests > usage.measuredRequests
      ? `; ${usage.measuredRequests} of ${usage.requests} responses measured` : "";
    const priced = usage.requests > usage.pricedRequests
      ? `; ${usage.pricedRequests} of ${usage.requests} responses priced` : "";
    usageSummaryText = `estimated month-to-date ${usage.cost} (${formatTokenCount(usage.inputTokens)} input tokens${cached} / ${formatTokenCount(usage.outputTokens)} output tokens)${measured}${priced}`;
  }
  const inner = `
        <span class="runtime-summary-name">
          <span class="runtime-status-dot ${missingKey ? "missing-key" : esc(status)}" aria-hidden="true"></span>
          <span class="runtime-summary-copy">
            <span>${esc(label)}</span>
            <span class="runtime-state">${esc(status)}</span>
          </span>
        </span>
        <span class="runtime-usage">${usageHtml}</span>`;
  return `
      <button class="runtime-summary runtime-summary-metered runtime-summary-host-inference" data-action="open-provider" data-provider="${esc(guideId)}" aria-label="${esc(`${label}: ${status}; ${usageSummaryText}. Open provider settings`)}">${inner}</button>`;
}

function toolUsagePanel() {
  if (!latestToolUsage) return '<p class="tool-spend-note">Tool spend is unavailable. Reopen to retry.</p>';
  const totals = tool => costUnits(tool.month_to_date);
  const tools = [...(latestToolUsage.tools || [])];
  tools.sort((a, b) => totals(a) === totals(b) ? a.display_name.localeCompare(b.display_name) : totals(a) > totals(b) ? -1 : 1);
  const cards = tools.map(tool => {
    const value = formatCost(tool.month_to_date);
    const detail = !tool.enabled ? "Disabled" : totals(tool) > 0n ? "Reported spend" : "Reporting costs";
    return `<button class="runtime-summary runtime-summary-metered tool-spend-card" data-action="open-provider" data-provider="tool:${esc(tool.tool_id)}" aria-label="${esc(`${tool.display_name}: ${value} month-to-date; ${detail}. Open integration guide`)}">
      <span class="runtime-summary-name"><span class="runtime-summary-copy"><span>${esc(tool.display_name)}</span><span class="runtime-state">${esc(detail)}</span></span></span>
      <span class="runtime-usage"><span class="runtime-stat runtime-stat-cost"><span class="runtime-stat-value">${esc(value)}</span><span class="runtime-stat-label">MTD</span></span></span>
    </button>`;
  }).join("");
  return `<p class="tool-spend-note">Reported spend · USD · This month (UTC)${toolUsageStale ? " · Refresh failed; showing previous figures" : ""}</p>${cards}`;
}

function usageWindows(account) {
  if (account.agent_runtime === "claude_code") {
    const usage = account.claude_usage || {};
    return {
      fiveHour: {
        usedPercent: usage.current_session_used_percent,
        resetsAt: usage.current_session_resets_at,
      },
      weekly: {
        usedPercent: usage.weekly_used_percent,
        resetsAt: usage.weekly_resets_at,
      },
      // The Fable-specific weekly window; shown only when the usage snapshot
      // carries one.
      fableWeekly: usage.fable_weekly_used_percent === undefined ? null : {
        label: "fable",
        usedPercent: usage.fable_weekly_used_percent,
        resetsAt: usage.fable_weekly_resets_at,
      },
    };
  }
  const limits = account.codex_usage?.rate_limits || {};
  const windows = Object.values(limits).filter(value => value && typeof value === "object");
  // Windows are identified by duration, not by primary/secondary position;
  // Number() tolerates a snapshot serializing durations as strings.
  const fiveHour = windows.find(window => Number(window.window_duration_mins) === 300);
  const weekly = windows.find(window => Number(window.window_duration_mins) === 10080);
  return {
    fiveHour: { usedPercent: fiveHour?.used_percent, resetsAt: fiveHour?.resets_at },
    weekly: { usedPercent: weekly?.used_percent, resetsAt: weekly?.resets_at },
    fableWeekly: null,
  };
}

function usageLabel(value) {
  return value !== undefined && value !== null && Number.isFinite(Number(value))
    ? `${Number(clampPercent(value))}% used`
    : "usage unavailable";
}

function resetCountdown(value, now = Date.now()) {
  if (value === undefined || value === null || value === "") return "";
  const numeric = Number(value);
  if (!Number.isFinite(numeric)) return "";
  const remaining = numeric * 1000 - now;
  if (remaining <= 0) return "due";
  const minutes = Math.max(1, Math.ceil(remaining / 60000));
  if (minutes >= 24 * 60) return `${Math.ceil(minutes / (24 * 60))}d`;
  if (minutes >= 60) return `${Math.ceil(minutes / 60)}h`;
  return `${minutes}m`;
}

function usageSummary(label, window) {
  const countdown = resetCountdown(window.resetsAt);
  const reset = countdown === "due" ? "; reset due" : countdown ? `; resets in ${countdown}` : "";
  return `${label} ${usageLabel(window.usedPercent)}${reset}`;
}

function usageRing(label, window) {
  const value = window.usedPercent;
  const available = value !== undefined && value !== null && Number.isFinite(Number(value));
  if (!available) return "";
  const percent = Number(clampPercent(value));
  const display = `${Math.round(percent)}`;
  const countdown = resetCountdown(window.resetsAt);
  const resetDescription = countdown === "due" ? "; reset due" : countdown ? `; resets in ${countdown}` : "";
  const title = `${label}: ${percent}% used${resetDescription}`;
  const thresholdClass = percent > 90 ? " usage-critical" : percent > 80 ? " usage-warning" : "";
  // One label line whether or not a countdown is known, so the ring block
  // (and with it the top bar) keeps a constant height.
  return `
    <span class="usage-ring${thresholdClass}">
      <svg viewBox="0 0 20 20" role="img" aria-label="${esc(title)}">
        <circle class="usage-ring-track" cx="10" cy="10" r="8.5" pathLength="100"></circle>
        <circle class="usage-ring-value" cx="10" cy="10" r="8.5" pathLength="100" stroke-dasharray="${percent} 100"></circle>
        <text x="10" y="10">${esc(display)}</text>
      </svg>
      <span class="usage-window">${esc(label)}${countdown ? ` · ${countdown}` : ""}</span>
    </span>`;
}

// Space out unsuccessful recovery reads; explicit login starts bypass this.
const OAUTH_RETRY_MS = 30000;
const oauthRetryAt = new Map();

async function showOauth(start, runtime) {
  if (runtime === "hermes") return; // no OAuth flow; credentials connect in the integration card
  const device = RUNTIME_PROVIDERS[runtime];
  if (!device) return;
  const { provider } = device;
  let target = document.querySelector(`[data-provider-oauth="${runtime}"]`);
  // Empty OAuth placeholders are display:none; measure their containing card.
  if (!target?.closest(".detail-card")?.getClientRects().length) return;
  if (!start && Date.now() < (oauthRetryAt.get(runtime) || 0)) return;
  // Set this before awaiting so overlapping health refreshes share the pause.
  // A late failed GET cannot reinstate a pause cleared by a successful start.
  oauthRetryAt.set(runtime, Date.now() + OAUTH_RETRY_MS);
  try {
    const route = runtime === "claude_code" ? "claude" : runtime;
    const login = await api(start ? "POST" : "GET", `/v1/agent-runtime/${route}-oauth-login`);
    oauthRetryAt.delete(runtime);
    // A health/network refresh may have replaced the card during the request.
    target = document.querySelector(`[data-provider-oauth="${runtime}"]`);
    if (!target) return;
    if (runtime === "claude_code") {
      setHtml(target, `<div class="oauth-card">
        <span>Claude Code login: open
        <a href="${esc(login.login_url)}" target="_blank" rel="noopener noreferrer">${esc(login.login_url)}</a>
        <span class="muted">(expires ${esc(login.expires_at)})</span></span>
        <button class="primary sm" data-action="complete-claude-login">Submit code</button></div>`);
      return;
    }
    setHtml(target, `<div class="oauth-card">
      <span>${esc(device.label)} login: enter code <b>${esc(login.device_code)}</b> at
      <a href="${esc(login.login_url)}" target="_blank" rel="noopener noreferrer">${esc(login.login_url)}</a>
      <span class="muted">(expires ${esc(login.expires_at)})</span>
      <span class="muted">After approving in your browser, wait ~5 seconds for the status to update.</span></span></div>`);
  } catch (error) {
    if (start) inlineMessage(document.querySelector(`[data-integration-message="${provider}"]`), error.message, true);
  }
}

export async function startLogin(runtime) { await showOauth(true, runtime); }

export async function completeClaudeLogin() {
  const code = prompt("Claude Code login code:");
  if (!code) return;
  try {
    await api("POST", "/v1/agent-runtime/claude-oauth-login/complete", { code });
    inlineMessage(document.querySelector('[data-integration-message="claude"]'), "Claude Code login submitted.");
    await refreshHealth();
  } catch (error) {
    inlineMessage(document.querySelector('[data-integration-message="claude"]'), error.message, true);
  }
}

export async function rebootHost() {
  if (!confirm("Reboot the host machine? This fails any agent work in progress right now. Queued work and durable data — threads, files, credentials, and policy — survive and resume after the host boots.")) return;
  try { await api("POST", "/v1/host-runtime/reboot"); notice("Reboot accepted; the host will be back shortly."); }
  catch (error) { notice(error.message, "error"); }
}
