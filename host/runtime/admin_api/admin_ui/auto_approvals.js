import { api } from "./api.js";
import { $, esc, formatUnixTime } from "./helpers.js";

const labels = { approved: "AI decision: approve", left_pending: "Left for your review", no_policy: "No policy found", failed: "Review failed" };
let data = null;
let page = 1;
let outcome = "";
let saving = false;
let editorTrigger = null;

export function autoReviewAnnotation(item) {
  if (item.kind !== "tool") return "";
  const review = item.auto_review;
  return `<div class="auto-review-note ${review?.outcome === "approved" ? "is-approved" : ""}">
    <div><strong>Auto-approval</strong><span>${esc(review ? labels[review.outcome] : "Not checked yet")}</span>
    ${review ? `<time>${esc(formatUnixTime(review.checked_at))}</time>` : ""}</div>
    ${review && review.outcome !== "no_policy" ? `<p>${esc(review.reason)}</p>` : ""}
    ${review?.approval_error ? `<p>Approval call failed or could not be confirmed: ${esc(review.approval_error)}</p>` : ""}
    ${item.status === "pending" ? `<button class="ghost" data-action="auto-policy" data-tool="${esc(item.tool_id)}" data-tool-action="${esc(item.action_id)}">${item.has_auto_policy ? "Edit" : "Set"} auto-approval policy</button>` : ""}
  </div>`;
}

export async function refreshAutoApprovals() {
  const root = $("auto-approval-page");
  if (!data) root.innerHTML = '<p class="muted">Loading policies and review history...</p>';
  try {
    const response = await api("GET", `/v1/auto-approvals?page=${page}&outcome=${outcome}`);
    data = response;
    page = data.history.page;
    render();
  } catch (error) {
    root.innerHTML = `<div class="approval-empty"><h2>Could not load auto-approval</h2><p>${esc(error.message)}</p><button data-auto-action="refresh">Try again</button></div>`;
  }
}

function toolName(id) { return data?.catalog.find(tool => tool.tool_id === id)?.name || id; }
function render() {
  const history = data.history;
  $("auto-approval-page").innerHTML = `
    <div class="auto-schedule"><div><span class="auto-status-dot ${data.available ? "ready" : ""}"></span><strong>${!data.available ? "Auto-approval is off" : data.quiet ? "Quiet hours" : "Scheduled reviews"}</strong>
    <p>${!data.available ? "Enable OpenAI under Home > Integrations > Host AI inference to turn on auto-approval. Add an API key there if you have not configured one." : data.next_review_at ? data.next_review_at * 1000 <= Date.now() ? "Review in progress" : `Next review ${esc(formatUnixTime(data.next_review_at))}` : "The next review is being scheduled."}</p></div>
    <div class="auto-schedule-details"><span>25 to 35 minutes between batches</span><span>Quiet hours 00:00 to 08:00 UTC</span><span>GPT-6 Sol</span></div></div>
    <section class="auto-section" aria-labelledby="auto-policies-title"><div class="auto-section-heading"><div><h2 id="auto-policies-title">Your policies <span class="muted">${data.policies.length}</span></h2><p class="muted">One policy per action. Save to activate, delete to stop.</p></div><button data-auto-action="add">Add policy</button></div>
    ${data.policies.length ? `<div class="auto-policy-list">${data.policies.map(policy => `<button class="auto-policy-row" data-auto-action="edit" data-tool="${esc(policy.tool_id)}" data-tool-action="${esc(policy.action_id)}"><span class="auto-policy-scope"><strong>${esc(toolName(policy.tool_id))}</strong><span>${esc(policy.action_id)}</span></span><span class="auto-policy-excerpt">${esc(policy.instructions)}</span><span class="auto-edit-label">Edit <span aria-hidden="true">↗</span></span></button>`).join("")}</div>` : `<div class="auto-empty"><h3>Let routine requests move forward</h3><p>Choose an action and describe what Kern may approve. Everything else stays in your review queue.</p><button class="ghost" data-auto-action="add">Create your first policy</button></div>`}</section>
    <section class="auto-section" aria-labelledby="auto-history-title"><div class="auto-section-heading"><div><h2 id="auto-history-title">Review history</h2><p class="muted">What was checked, the policy used, and what happened.</p></div><label class="auto-filter">Outcome<select id="auto-history-filter">${[["", "All outcomes"], ...Object.entries(labels)].map(([value, label]) => `<option value="${value}"${value === outcome ? " selected" : ""}>${label}</option>`).join("")}</select></label></div>
    ${history.items.length ? `<div class="auto-history-list">${history.items.map(row => `<details class="auto-history-row"><summary><span class="auto-history-title"><span class="auto-outcome ${row.outcome === "approved" ? "is-approved" : ""}">${esc(labels[row.outcome])}</span><strong>${esc(row.summary)}</strong><span class="muted">${esc(toolName(row.tool_id))} · ${esc(row.action_id)}</span></span><time>${esc(formatUnixTime(row.checked_at))}</time></summary><div class="auto-history-detail"><p>${esc(row.reason)}</p>${row.policy ? `<h3>Policy at the time of review</h3><p class="auto-policy-text">${esc(row.policy)}</p>${row.model ? `<p class="muted">Reviewed by ${esc(row.model)}</p>` : ""}` : ""}${row.outcome === "approved" ? `<h3>Request status</h3><p>${esc(row.status === "executed" ? "Succeeded" : row.status === "failed" ? "Execution failed" : row.status === "denied" ? "Denied" : row.status === "approved" ? "Execution not yet confirmed" : "Pending")}${row.result ? `: ${esc(row.result)}` : ""}</p>` : ""}${row.approval_error ? `<p>Approval call failed or could not be confirmed: ${esc(row.approval_error)}</p>` : ""}<button class="ghost" data-auto-action="request" data-tool="${esc(row.tool_id)}" data-approval="${esc(row.approval_id)}">View exact request</button><pre class="approval-payload" hidden></pre></div></details>`).join("")}</div>` : `<div class="auto-empty"><h3>${outcome ? "No matching reviews" : "No reviews yet"}</h3><p>${outcome ? "Choose another outcome to browse review history." : "Scheduled checks will appear here, including requests left pending."}</p></div>`}
    <nav class="approval-pagination" aria-label="Auto-approval history pages"><span class="muted">${history.total} reviews</span><div><button class="ghost" data-auto-action="page" data-page="${page - 1}"${page <= 1 ? " disabled" : ""}>Previous</button><span>Page ${page} of ${history.pages}</span><button class="ghost" data-auto-action="page" data-page="${page + 1}"${page >= history.pages ? " disabled" : ""}>Next</button></div></nav></section>`;
}

export async function openAutoPolicy(toolId = "", actionId = "") {
  editorTrigger = document.activeElement;
  data = await api("GET", `/v1/auto-approvals?page=${page}&outcome=${outcome}`);
  const dialog = $("auto-policy-dialog");
  dialog.innerHTML = `<form id="auto-policy-form"><div class="auto-section-heading"><div><h2>Auto-approval policy</h2><p class="muted">Describe when Kern may approve this action.</p></div><button type="button" class="ghost" data-auto-action="close" aria-label="Close policy editor">✕</button></div>
    <div class="auto-policy-selectors"><label>Tool<select id="auto-policy-tool" required><option value="">Choose a tool</option>${data.catalog.map(tool => `<option value="${esc(tool.tool_id)}">${esc(tool.name)}</option>`).join("")}</select></label><label>Action<select id="auto-policy-action" required><option value="">Choose an action</option></select></label></div>
    <p id="auto-action-description" class="muted"></p><label for="auto-policy-instructions">When may Kern approve automatically?</label><textarea id="auto-policy-instructions" rows="7" maxlength="8000" required placeholder="Allow routine follow-ups from the support account. Leave discounts, attachments and new recipients for my review."></textarea>
    <p class="muted auto-editor-hint">Applies to unchecked pending requests and future requests for this action, across accounts unless your instructions restrict them. Each request is checked once, even if you edit the policy.</p>
    <p id="auto-policy-error" class="error" role="alert"></p><div class="auto-editor-footer"><button type="button" class="danger ghost" data-auto-action="delete" id="auto-policy-delete" hidden>Delete policy</button><span></span><button type="button" class="ghost" data-auto-action="close">Cancel</button><button type="submit">Save policy</button></div><p class="muted auto-editor-hint">Saving takes effect at the next scheduled review. It does not approve a request immediately.</p></form>`;
  $("auto-policy-tool").value = toolId;
  updateActions(actionId);
  if (toolId && actionId) {
    $("auto-policy-tool").disabled = true;
    $("auto-policy-action").disabled = true;
  }
  dialog.showModal();
  $("auto-policy-instructions").focus();
}

function updateActions(selected = "") {
  const tool = data.catalog.find(tool => tool.tool_id === $("auto-policy-tool").value);
  $("auto-policy-action").innerHTML = '<option value="">Choose an action</option>' + (tool?.actions || []).map(action => `<option value="${esc(action.id)}">${esc(action.id)}${data.policies.some(p => p.tool_id === tool.tool_id && p.action_id === action.id) ? " (policy exists)" : ""}</option>`).join("");
  $("auto-policy-action").value = selected;
  loadPolicy();
}
function editorBody() { return { tool_id: $("auto-policy-tool").value, action_id: $("auto-policy-action").value, instructions: $("auto-policy-instructions").value }; }
function loadPolicy() {
  const body = editorBody();
  const policy = data.policies.find(p => p.tool_id === body.tool_id && p.action_id === body.action_id);
  $("auto-policy-instructions").value = policy?.instructions || "";
  $("auto-policy-delete").hidden = !policy;
  $("auto-action-description").textContent = data.catalog.find(t => t.tool_id === body.tool_id)?.actions.find(a => a.id === body.action_id)?.description || "";
}
function editorBusy(value) {
  saving = value;
  $("auto-policy-dialog").querySelectorAll("button, textarea").forEach(node => { node.disabled = value; });
}
async function savePolicy(remove = false) {
  if (saving) return;
  if (!remove && !$("auto-policy-form").reportValidity()) return;
  if (remove && !confirm("Delete this policy? Pending and future requests for this action will need manual approval.")) return;
  editorBusy(true);
  try {
    const body = editorBody();
    if (remove) delete body.instructions;
    await api(remove ? "DELETE" : "PUT", "/v1/auto-approvals/policy", body);
    $("auto-policy-dialog").close();
    document.dispatchEvent(new CustomEvent("auto-policy-saved"));
  } catch (error) { $("auto-policy-error").textContent = error.message; }
  finally { editorBusy(false); }
}

document.addEventListener("submit", event => {
  if (event.target.id !== "auto-policy-form") return;
  event.preventDefault();
  savePolicy();
});
document.addEventListener("change", event => {
  if (event.target.id === "auto-policy-tool") updateActions();
  if (event.target.id === "auto-policy-action") loadPolicy();
  if (event.target.id === "auto-history-filter") { outcome = event.target.value; page = 1; refreshAutoApprovals(); }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-auto-action]");
  if (!button || button.disabled) return;
  try {
    switch (button.dataset.autoAction) {
      case "refresh": return refreshAutoApprovals();
      case "add": return await openAutoPolicy();
      case "edit": return await openAutoPolicy(button.dataset.tool, button.dataset.toolAction);
      case "page": page = Number(button.dataset.page); return refreshAutoApprovals();
      case "close": $("auto-policy-dialog").close(); editorTrigger?.focus(); return;
      case "delete": return savePolicy(true);
      case "request": {
        const pre = button.nextElementSibling;
        pre.hidden = false;
        pre.textContent = "Loading request...";
        const response = await api("GET", `/v1/tools/${encodeURIComponent(button.dataset.tool)}/approvals/${encodeURIComponent(button.dataset.approval)}`);
        pre.textContent = JSON.stringify(response.approval.payload, null, 2);
        return;
      }
    }
  } catch (error) {
    if ($("auto-policy-dialog").open) $("auto-policy-error").textContent = error.message;
    else $("approval-feedback").textContent = error.message;
  }
});

document.addEventListener("cancel", event => {
  if (event.target.id === "auto-policy-dialog" && saving) event.preventDefault();
}, true);
