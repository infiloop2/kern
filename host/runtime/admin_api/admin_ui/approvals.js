// One operator queue. Decisions use the existing authenticated per-source APIs.
import { refreshAutoApprovals, autoReviewAnnotation } from "./auto_approvals.js";
import { api } from "./api.js";
import { $, esc, badge, formatUnixTime } from "./helpers.js";

let view = "pending";
let page = 1;
let current = null;
let busy = false;
let loading = false;
let generation = 0;
let badgeLoading = false;
let counts = { pending_count: 0, history_count: 0 };
const keyOf = item => `${item.kind}:${item.id}`;
const disabled = () => busy || loading ? " disabled" : "";
const riskScoreLabels = {
  commits_money_or_obligation: "Commits money or obligation",
  sensitive_data: "Sensitive data",
  summary_mismatch: "Summary mismatch",
};

function riskAnnotation(item) {
  const entries = Object.entries(riskScoreLabels).map(([id, label]) => {
    const score = item.risk_scores?.[id];
    if (!Number.isFinite(score) || score < 0 || score > 1) return null;
    const percent = Math.round(score * 100);
    const level = score > 0.66 ? "high" : score > 0.33 ? "moderate" : "low";
    return `<div class="approval-risk-score ${level}" aria-label="${esc(label)} risk ${percent}%">
      <span class="approval-risk-label">${esc(label)}</span>
      <progress class="approval-risk-track" value="${percent}" max="100" aria-hidden="true"></progress>
      <strong>${percent}%</strong>
    </div>`;
  });
  if (entries.some(entry => entry === null)) return "";
  return `<div class="approval-risk-scores" role="group" aria-label="TypeSafe Jev risk scores">${entries.join("")}</div>`;
}

function clearLoadError() {
  $("approval-feedback").querySelector("[data-approval-load-error]")?.remove();
}

function updateCounts(data) {
  counts = { pending_count: data.pending_count, history_count: data.history_count };
  for (const view of ["pending", "history"]) {
    const counter = document.querySelector(`#approval-tabs [data-view="${view}"] span`);
    if (counter) counter.textContent = counts[`${view}_count`];
  }
  const count = data.pending_count;
  const node = $("approval-nav-count");
  node.textContent = count > 99 ? "99+" : String(count);
  node.hidden = count === 0;
  $("tab-approvals").setAttribute("aria-label", `Approvals, ${count} pending`);
}

export async function pollApprovalBadge() {
  if (badgeLoading) return;
  badgeLoading = true;
  try {
    const data = await api("GET", "/v1/approvals?view=pending&page=1");
    updateCounts(data);
  } finally {
    badgeLoading = false;
  }
}

function render() {
  const data = current;
  $("approval-tabs").innerHTML = ["pending", "history", "auto"].map(value => `<button data-action="approval-view" data-view="${value}" aria-pressed="${view === value}"${disabled()}>${value === "auto" ? "Auto-approval" : value === "pending" ? "Pending" : "History"}${value === "auto" ? "" : `<span>${counts[`${value}_count`]}</span>`}</button>`).join("");
  document.querySelector('#panel-approvals [data-action="approval-refresh"]').disabled = busy || loading;
  $("approval-toolbar").innerHTML = view === "pending" && data?.items.length
    ? `<div class="approval-bulk-actions"><button data-action="approval-bulk" data-decision="approve"${disabled()}>Approve all (${data.items.length})</button><button class="danger ghost" data-action="approval-bulk" data-decision="deny"${disabled()}>Deny all (${data.items.length})</button></div>`
    : "";
  $("auto-approval-page").hidden = view !== "auto";
  $("approval-list").hidden = view === "auto";
  $("approval-pagination").hidden = view === "auto";
  if (view === "auto") return;
  if (!data) {
    $("approval-list").innerHTML = `<div class="approval-empty">${loading ? "Loading approvals..." : "Approvals could not be loaded. Use Refresh to try again."}</div>`;
    return;
  }
  $("approval-list").innerHTML = data.items.length ? data.items.map(item => `
    <article class="approval-card" data-approval-key="${esc(keyOf(item))}">
      <div class="approval-card-top"><span class="approval-source">${esc(item.source)}${item.account_label ? `<span class="muted"> / ${esc(item.account_label)}</span>` : ""}${item.connection_id ? `<span class="muted"> · ${esc(item.connection_id)}</span>` : ""}</span>${badge(item.status)}</div>
      <h2>${esc(item.summary)}</h2>
      ${riskAnnotation(item)}
      ${autoReviewAnnotation(item)}
      <div class="approval-card-meta"><time>${esc(formatUnixTime(view === "pending" ? item.created_at : item.updated_at))}</time><span>${esc(item.kind === "github_push" ? `push-${item.id}` : item.action_id)}</span></div>
      <details data-approval-details="${esc(keyOf(item))}"><summary>${item.kind === "tool" ? "View exact request" : "View changes"}</summary><pre class="approval-payload">${item.kind === "github_push" ? esc(JSON.stringify({ refs: item.ref_updates, paths: item.changed_paths }, null, 2)) : ""}</pre></details>
      ${item.result ? `<div class="approval-result">${esc(typeof item.result === "string" ? item.result : JSON.stringify(item.result))}</div>` : ""}
      ${view === "pending" ? `<div class="approval-card-bottom"><span class="approval-progress" role="status"></span><div class="approval-decisions"><button data-action="approval-decide" data-key="${esc(keyOf(item))}" data-decision="approve"${disabled()}>Approve</button><button class="danger ghost" data-action="approval-decide" data-key="${esc(keyOf(item))}" data-decision="deny"${disabled()}>Deny</button></div></div>` : ""}
    </article>`).join("") : `<div class="approval-empty"><span class="approval-empty-icon" aria-hidden="true">✓</span><h2>${view === "pending" ? "All caught up" : "No decisions yet"}</h2><p class="muted">${view === "pending" ? "New requests will appear here for your review." : "Approved and denied requests will appear here."}</p></div>`;
  const start = data.total ? (data.page - 1) * data.page_size + 1 : 0;
  $("approval-pagination").innerHTML = `<span class="muted">${start} to ${Math.min(data.page * data.page_size, data.total)} of ${data.total}</span><div><button class="ghost" data-action="approval-page" data-page="${data.page - 1}"${data.page <= 1 ? " disabled" : disabled()}>Previous</button><span>Page ${data.page} of ${data.pages}</span><button class="ghost" data-action="approval-page" data-page="${data.page + 1}"${data.page >= data.pages ? " disabled" : disabled()}>Next</button></div>`;
}

export async function refreshApprovals() {
  if (busy || loading) return;
  if (view === "auto") { render(); return refreshAutoApprovals(); }
  const request = ++generation;
  loading = true;
  render();
  try {
    const data = await api("GET", `/v1/approvals?view=${view}&page=${page}`);
    if (request !== generation) return;
    current = data;
    page = data.page;
    updateCounts(data);
    clearLoadError();
    $("approval-updates").hidden = true;
  } catch (error) {
    clearLoadError();
    const message = document.createElement("p");
    message.dataset.approvalLoadError = "";
    message.textContent = `Could not load approvals: ${error.message}`;
    $("approval-feedback").append(message);
    if (current) page = current.page;
  } finally {
    loading = false;
    render();
  }
}

export async function pollApprovals() {
  if (view === "auto") return;
  if (busy || loading) return;
  const request = generation;
  const data = await api("GET", `/v1/approvals?view=${view}&page=${page}`);
  if (request !== generation || busy || loading) return;
  updateCounts(data);
  // Keep the reviewed page stable, including open payloads and focused buttons.
  if (JSON.stringify(data) !== JSON.stringify(current)) $("approval-updates").hidden = false;
}

export function changeApprovalView(next) {
  if (busy || loading || !["pending", "history", "auto"].includes(next) || next === view) return;
  view = next;
  page = 1;
  current = null;
  $("approval-feedback").textContent = "";
  return refreshApprovals();
}

export function changeApprovalPage(next) {
  if (busy || loading || !current || next < 1 || next > current.pages) return;
  page = next;
  return refreshApprovals();
}

function progress(item, message) {
  const card = [...$("approval-list").children].find(node => node.dataset.approvalKey === keyOf(item));
  if (card) card.querySelector(".approval-progress").textContent = message;
}

// A decision can finish after the browser loses its response. Read the saved
// status before reporting a failed action; never send the decision a second time.
async function recordedOutcome(item, decision) {
  try {
    const record = item.kind === "tool"
      ? (await api("GET", `/v1/tools/${encodeURIComponent(item.tool_id)}/approvals/${encodeURIComponent(item.id)}`)).approval
      : (await api("GET", "/v1/network-tools/github-pending-pushes")).pending_pushes.find(push => push.id === item.id);
    const success = item.kind === "tool"
      ? (decision === "approve" ? "executed" : "denied")
      : (decision === "approve" ? "approved" : "rejected");
    const opposite = item.kind === "tool"
      ? (decision === "approve" ? "denied" : "executed")
      : (decision === "approve" ? "rejected" : "approved");
    if (record?.status === success) return { status: "completed" };
    if (record?.status === opposite) return {
      status: "resolved_differently",
      message: decision === "approve" ? "Already denied in another session." : "Already approved in another session.",
    };
    if (record?.status === "failed") return { status: "failed", error: record.result || record.detail || "Action failed." };
  } catch (_) {
    // A second read can fail too; the decision's outcome is then unknown.
  }
  return { status: "unconfirmed" };
}

async function decide(items, decision, confirmBulk = false) {
  if (busy || loading || !items.length || !["approve", "deny"].includes(decision)) return;
  const verb = decision === "approve" ? "Approve" : "Deny";
  if (confirmBulk && !confirm(`${verb} ${items.length === 1 ? "this request" : `these ${items.length} visible requests`}? ${decision === "approve" ? "Approved actions run immediately, exactly as recorded." : "Denied requests will not run."}`)) return;
  busy = true;
  ++generation;
  // Freeze the exact visible requests before any network call or refresh.
  render();
  const outcomes = [];
  for (const item of items) progress(item, "Queued");
  async function run(item) {
    progress(item, decision === "approve" ? "Approving..." : "Denying...");
    let outcome;
    try {
      const path = item.kind === "tool"
        ? `/v1/tools/${encodeURIComponent(item.tool_id)}/approvals/${encodeURIComponent(item.id)}/${decision}`
        : `/v1/network-tools/github-pending-pushes/${encodeURIComponent(item.id)}/${decision === "deny" ? "reject" : "approve"}`;
      const response = await api("POST", path, {});
      outcome = response.result?.status === "failed" || response.pending_push?.status === "failed"
        ? { status: "failed", error: response.result?.error || response.pending_push?.detail || "Approved action failed" }
        : { status: "completed" };
    } catch (error) {
      outcome = await recordedOutcome(item, decision);
    }
    progress(item, outcome.status === "completed" ? (decision === "approve" ? "Approved" : "Denied")
      : outcome.status === "failed" ? outcome.error
      : outcome.status === "resolved_differently" ? outcome.message : "Outcome not confirmed");
    outcomes.push({ item, ...outcome });
    $("approval-feedback").textContent = `${outcomes.length} of ${items.length} requests completed.`;
  }
  // The browser service runs one action at a time. Keep its approvals in order;
  // unrelated tools and GitHub decisions can still run independently.
  await Promise.all([
    ...items.filter(item => item.kind === "tool" && item.tool_id !== "browser").map(run),
    (async () => { for (const item of items.filter(item => item.kind === "tool" && item.tool_id === "browser")) await run(item); })(),
    (async () => { for (const item of items.filter(item => item.kind === "github_push")) await run(item); })(),
  ]);
  busy = false;
  const failures = outcomes.filter(outcome => outcome.status === "failed");
  const resolvedDifferently = outcomes.filter(outcome => outcome.status === "resolved_differently");
  const unconfirmed = outcomes.filter(outcome => outcome.status === "unconfirmed");
  const completed = outcomes.length - failures.length - resolvedDifferently.length - unconfirmed.length;
  $("approval-feedback").innerHTML = `<p>${completed} ${decision === "approve" ? "approved" : "denied"}${failures.length ? `, ${failures.length} failed` : ""}${resolvedDifferently.length ? `, ${resolvedDifferently.length} resolved differently` : ""}${unconfirmed.length ? `, ${unconfirmed.length} unconfirmed` : ""}.</p>${failures.length ? `<ul>${failures.map(({ item, error }) => `<li>${esc(item.source)}: ${esc(item.summary)}. ${esc(error)}</li>`).join("")}</ul>` : ""}${resolvedDifferently.length ? `<ul>${resolvedDifferently.map(({ item, message }) => `<li>${esc(item.source)}: ${esc(item.summary)}. ${esc(message)}</li>`).join("")}</ul>` : ""}${unconfirmed.length ? `<p>Could not confirm the outcome for ${unconfirmed.length} request${unconfirmed.length === 1 ? "" : "s"}. Check History before taking further action.</p>` : ""}`;
  await refreshApprovals();
}

export function decideApproval(key, decision) {
  if (busy || loading || !["approve", "deny"].includes(decision)) return;
  const item = current?.items.find(candidate => keyOf(candidate) === key);
  if (item?.status !== "pending") return;
  return decide([item], decision);
}

export function decideVisibleApprovals(decision) {
  if (view === "pending") return decide([...(current?.items || [])], decision, true);
}

document.addEventListener("toggle", async event => {
  const details = event.target;
  if (!(details instanceof HTMLDetailsElement) || !details.open || !details.dataset.approvalDetails) return;
  const item = current?.items.find(candidate => keyOf(candidate) === details.dataset.approvalDetails);
  if (!item || item.kind !== "tool" || details.dataset.loaded) return;
  details.dataset.loaded = "1";
  const pre = details.querySelector("pre");
  pre.textContent = "Loading request...";
  try {
    const response = await api("GET", `/v1/tools/${encodeURIComponent(item.tool_id)}/approvals/${encodeURIComponent(item.id)}`);
    pre.textContent = JSON.stringify(response.approval.payload, null, 2);
  } catch (error) {
    pre.textContent = `Could not load request: ${error.message}. Close and reopen to retry.`;
    delete details.dataset.loaded;
  }
}, true);

document.addEventListener("auto-policy-saved", () => refreshApprovals());
