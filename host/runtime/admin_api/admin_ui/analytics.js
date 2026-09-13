// First-party token accounting. Seven UTC calendar days, including today.
import { api } from "./api.js";
import { $, esc, formatTokenCount } from "./helpers.js";

const fields = ["input_tokens", "cached_input_tokens", "cache_write_tokens", "output_tokens"];
const labels = ["Input", "Cached input", "Cache write", "Output"];
const kinds = { chats: "Chats", apps: "Apps", schedules: "Schedules" };
const runtimes = { codex: "Codex", "codex-2": "Codex 2", claude_code: "Claude Code", grok: "Grok", hermes: "Hermes" };
let report = null;
let kind = "all";
let thread = "";
let page = 0;
let sequence = 0;

function total(tokens) {
  return fields.reduce((sum, key) => sum + (tokens[key] ?? 0), 0);
}

function aggregate(rows) {
  const tokens = Object.fromEntries(fields.map(key => [key, null]));
  const coverage = Object.fromEntries(fields.map(key => [key, 0]));
  let turns = 0;
  for (const row of rows) {
    turns += row.turns;
    for (const key of fields) {
      if (row.tokens[key] !== null) tokens[key] = (tokens[key] ?? 0) + row.tokens[key];
      coverage[key] += row.measured_turns[key];
    }
  }
  return { tokens, coverage, turns };
}

function count(value) {
  return value === null ? "—" : formatTokenCount(value);
}

function metric(value, known, turns) {
  const partial = known < turns && value !== null;
  return `<span title="${esc(value === null ? "Usage unavailable" : `${value.toLocaleString()} tokens · ${known} of ${turns} turns measured`)}">${esc(count(value))}${partial ? "*" : ""}</span>`;
}

function render() {
  if (!report) return;
  if (thread && !report.groups.some(row => row.thread_id === thread)) thread = "";
  const all = report.groups.filter(row => kind === "all" || row.kind === kind);
  const rows = all.filter(row => !thread || row.thread_id === thread);
  const totals = aggregate(rows);
  $("analytics-cards").innerHTML = fields.map((key, i) => `<div class="stat-tile"><div class="stat-label">${labels[i]}</div><div class="stat-value">${metric(totals.tokens[key], totals.coverage[key], totals.turns)}</div></div>`).join("");
  const selected = report.groups.find(row => row.thread_id === thread);
  $("analytics-focus").hidden = !selected;
  $("analytics-focus-name").textContent = selected?.name || "";
  $("analytics-turns").textContent = `${totals.turns.toLocaleString()} turns · Today and the previous 6 days · UTC`;
  $("analytics-empty").hidden = rows.length > 0;
  $("analytics-detail").hidden = rows.length === 0;
  $("analytics-updated").textContent = `Updated ${new Date(report.until).toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}`;

  const days = report.days.map(day => {
    const matching = rows.filter(row => row.day === day);
    const byKind = Object.fromEntries(Object.keys(kinds).map(key => [key, total(aggregate(matching.filter(row => row.kind === key)).tokens)]));
    return { day, byKind, total: Object.values(byKind).reduce((a, b) => a + b, 0) };
  });
  const max = Math.max(1, ...days.map(day => day.total));
  $("analytics-chart").innerHTML = days.map(day => `<div class="analytics-day"><div class="analytics-bar-value">${esc(new Intl.NumberFormat("en", { notation: "compact", maximumFractionDigits: 0 }).format(day.total))}</div><div class="analytics-bar-track"><div class="analytics-stack">${Object.entries(kinds).map(([key, label]) => `<div class="analytics-segment ${key}" title="${esc(`${day.day}: ${label}, ${day.byKind[key].toLocaleString()} measured tokens`)}"></div>`).join("")}</div></div><div>${esc(day.day.slice(5))}</div></div>`).join("");
  // Property assignments work under the host's strict style CSP; HTML style
  // attributes are intentionally forbidden.
  $("analytics-chart").querySelectorAll(".analytics-stack").forEach((stack, i) => {
    stack.style.height = `${days[i].total / max * 100}%`;
    [...stack.children].forEach((segment, j) => {
      segment.style.flex = String(days[i].byKind[Object.keys(kinds)[j]]);
    });
  });
  $("analytics-chart").setAttribute("aria-label", days.map(day => `${day.day}: ${day.total.toLocaleString()} measured tokens`).join("; "));

  const providerKeys = [...new Set(rows.map(row => `${row.runtime}\n${row.model}`))];
  $("analytics-providers").innerHTML = providerKeys.map(key => {
    const [runtime, model] = key.split("\n");
    const summary = aggregate(rows.filter(row => row.runtime === runtime && row.model === model));
    return { runtime, model, summary };
  }).sort((a, b) => total(b.summary.tokens) - total(a.summary.tokens)).map(({ runtime, model, summary }) => `<div class="analytics-provider"><div><strong>${esc(runtimes[runtime] || runtime)}</strong><span class="muted">${esc(model)}</span></div><div class="analytics-provider-counts">${fields.map((key, i) => `<span><small>${labels[i]}</small>${metric(summary.tokens[key], summary.coverage[key], summary.turns)}</span>`).join("")}</div></div>`).join("");

  const byThread = new Map();
  for (const row of rows) {
    if (!byThread.has(row.thread_id)) byThread.set(row.thread_id, []);
    byThread.get(row.thread_id).push(row);
  }
  const ranked = [...byThread.values()].map(matching => ({
    ...matching[0], summary: aggregate(matching),
  })).sort((a, b) => total(b.summary.tokens) - total(a.summary.tokens));
  page = Math.min(page, Math.max(0, Math.ceil(ranked.length / 20) - 1));
  $("analytics-threads").innerHTML = ranked.slice(page * 20, page * 20 + 20).map(row => {
    const route = row.kind === "apps" ? "apps" : "chat";
    const conversation = row.active
      ? ` · <a href="#${route}/${encodeURIComponent(row.thread_id)}">Open conversation</a>`
      : ` · ${row.kind === "schedules" ? "Inactive" : "Archived"}`;
    return `<tr><td><button class="analytics-thread-name" data-analytics-thread="${esc(row.thread_id)}">${esc(row.name)}</button><small>${kinds[row.kind]}${conversation}</small></td><td>${row.summary.turns}</td>${fields.map(key => `<td>${metric(row.summary.tokens[key], row.summary.coverage[key], row.summary.turns)}</td>`).join("")}</tr>`;
  }).join("");
  $("analytics-pagination").hidden = ranked.length <= 20;
  $("analytics-page").textContent = `${page + 1} / ${Math.max(1, Math.ceil(ranked.length / 20))}`;
  $("analytics-prev").disabled = page === 0;
  $("analytics-next").disabled = (page + 1) * 20 >= ranked.length;
}

export async function refreshAnalytics() {
  const current = ++sequence;
  $("analytics-status").textContent = "Loading usage…";
  try {
    const result = await api("GET", "/v1/analytics");
    if (current !== sequence) return;
    report = result;
    render();
    $("analytics-status").textContent = "";
  } catch (error) {
    if (current === sequence) $("analytics-status").textContent = error.message || "Could not load usage. Try Refresh.";
  }
}

$("analytics-refresh").addEventListener("click", refreshAnalytics);
$("analytics-kind").addEventListener("change", event => {
  kind = event.target.value;
  thread = "";
  page = 0;
  render();
});
$("analytics-clear").addEventListener("click", () => { thread = ""; page = 0; render(); });
$("analytics-threads").addEventListener("click", event => {
  const button = event.target.closest("button[data-analytics-thread]");
  if (!button) return;
  thread = button.dataset.analyticsThread;
  page = 0;
  render();
  $("panel-analytics").scrollIntoView({ block: "start" });
});
$("analytics-prev").addEventListener("click", () => { page -= 1; render(); });
$("analytics-next").addEventListener("click", () => { page += 1; render(); });
