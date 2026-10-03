import { api } from "./api.js";
import { $, runtimeLabel } from "./helpers.js";
import { layoutAgents } from "./swarm_layout.js";

const TYPES = { "on-demand": "On-demand", app: "App", standing: "Standing", spawned: "Spawned", operator: "Human" };
const STATES = { busy: "Working", failed: "Error", idle: "Idle" };
let snapshot = null;
let agentById = new Map();
let interactions = [];
let selectedId = null;
let selectedEdge = null;
let activeFilter = "all";
let search = "";
let loading = false;
let refreshPending = false;
let bound = false;
let refreshError = "";
let navigationError = "";
let layout = null;
let layoutKey = "";
let scale = 1;
const cards = new Map();
const svgNS = "http://www.w3.org/2000/svg";

function node(tag, className, value = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = value;
  return element;
}
function svg(tag, attrs) {
  const element = document.createElementNS(svgNS, tag);
  for (const [key, value] of Object.entries(attrs)) element.setAttribute(key, String(value));
  return element;
}
// All paths are repository-authored. Names and AI text only enter textContent.
const operator = { thread_id: "operator", name: "Operator", kind: "operator", state: "", pending_approval_count: 0 };
function character(agent) {
  if (agent.kind === "operator") return `<svg viewBox="0 0 64 76" aria-hidden="true"><circle class="operator-head" cx="32" cy="22" r="13"/><path class="operator-body" d="M9 67v-9a23 23 0 0 1 46 0v9z"/><path class="operator-smile" d="M26 25q6 6 12 0"/></svg>`;
  const pose = agent.state === "busy" ? "busy" : agent.state === "failed" ? "failed" : agent.pending_approval_count > 0 ? "needs-human" : "idle";
  const variant = Number(agent.thread_id.split("-").pop()) % 3;
  const eyes = pose === "failed"
    ? '<path d="m17 22 4 4m0-4-4 4m12-4 4 4m0-4-4 4"/>'
    : pose === "idle"
    ? '<path d="M17 24q2 2 4 0m8 0q2 2 4 0"/>'
    : '<ellipse cx="19" cy="24" rx="2" ry="3"/><ellipse cx="31" cy="24" rx="2" ry="3"/>';
  return `<svg viewBox="0 0 50 60" aria-hidden="true">
    <ellipse class="critter-shadow" cx="25" cy="56" rx="17" ry="3"/>
    <g class="critter-body">
      <path class="critter-foot" d="M16 49v5h-4m22-5v5h4"/>
      <path class="critter-arm arm-left" d="M12 38 7 42"/>
      <g class="arm-right"><path class="critter-arm" d="${pose === "needs-human" ? 'M38 37q9-3 7-14' : 'M38 38 43 42'}"/></g>
      <rect class="critter-suit" x="11" y="30" width="28" height="21" rx="11"/>
      <circle class="critter-badge" cx="25" cy="41" r="2"/>
      <rect class="critter-head" x="8" y="7" width="34" height="30" rx="14"/>
      <circle class="critter-ear" cx="8" cy="23" r="3"/><circle class="critter-ear" cx="42" cy="23" r="3"/>
      <rect class="critter-face" x="12" y="15" width="26" height="18" rx="8"/>
      <g class="critter-eyes">${eyes}</g>
      ${pose === "failed" ? '<path class="critter-frown" d="M22 30q3-3 6 0"/>' : ''}
      ${pose === "busy" ? '<g class="critter-laptop"><path d="M13 39h24l-2 11H15z"/><path d="M11 51h28"/><circle cx="25" cy="45" r="1.5"/></g>' : ''}
      ${pose === "needs-human" ? '<g class="critter-question"><circle cx="43" cy="10" r="7"/><text x="43" y="13" text-anchor="middle">?</text></g>' : ''}
      ${pose === "failed" ? '<g class="critter-failed"><circle cx="43" cy="10" r="7"/><text x="43" y="13" text-anchor="middle">!</text></g>' : ''}
      ${pose === "idle" && variant === 0 ? '<text class="critter-zzz" x="40" y="10">z</text>' : ''}
    </g></svg>`;
}

function description(agent) {
  if (agent.kind === "operator") return "Your messages to the swarm";
  if (agent.kind === "on-demand") return agent.task || "No current task summary";
  if (agent.kind === "spawned") {
    const parent = agentById.get(agent.spawned_by_thread_id);
    return `Delegated by ${parent?.name || agent.spawned_by_thread_id || "another agent"}`;
  }
  return agent.purpose || "No purpose set";
}
function matches(agent) {
  const status = activeFilter === "all" || (activeFilter === "needs-human"
    ? agent.pending_approval_count > 0 : agent.state === activeFilter);
  return status && `${agent.name} ${TYPES[agent.kind]} ${description(agent)}`.toLowerCase().includes(search);
}
function time(value) { return new Date(value).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }); }
function renderError() {
  $("swarm-error").textContent = [refreshError, navigationError].filter(Boolean).join(" ");
  $("swarm-error").hidden = !$("swarm-error").textContent;
}
export function setSwarmVisible(visible) {
  const wasVisible = document.body.classList.contains("swarm-open");
  document.body.classList.toggle("swarm-open", visible);
  document.querySelector(".topbar").inert = visible;
  $("sidebar").inert = visible;
  if (visible) $("swarm-close").focus({ preventScroll: true });
  else if (wasVisible) $(window.matchMedia("(max-width: 860px)").matches ? "mobile-nav-toggle" : "tab-swarm").focus({ preventScroll: true });
}
function selectAgent(id, focus = false) {
  selectedId = id;
  selectedEdge = null;
  render();
  if (focus) cards.get(id)?.scrollIntoView({ block: "center", inline: "center" });
}
async function openAgent(agent) {
  try {
    const opened = await window.KernHost.openWorkspace(agent.kind === "app" ? "apps" : "chat", agent.thread_id);
    if (opened === false) throw new Error("the thread is no longer available");
    navigationError = "";
  } catch (error) { navigationError = `Could not open ${agent.name}: ${error.message}`; }
  renderError();
}
function renderDetails() {
  const root = $("swarm-detail");
  root.replaceChildren();
  const hasSelection = Boolean(selectedEdge || agentById.has(selectedId));
  root.classList.toggle("has-selection", hasSelection);
  if (hasSelection) {
    const close = node("button", "swarm-detail-close", "×");
    close.setAttribute("aria-label", "Close agent details");
    close.addEventListener("click", () => { selectedId = null; selectedEdge = null; render(); });
    root.append(close);
  }
  if (selectedEdge) {
    const name = id => agentById.get(id)?.name || id;
    root.append(node("h2", "", "Communication"), node("p", "", `${name(selectedEdge.sender_thread_id)} → ${name(selectedEdge.target_thread_id)}`),
      node("p", "", `${selectedEdge.count} accepted messages · last 7 UTC days`));
    return;
  }
  const agent = agentById.get(selectedId);
  if (!agent) {
    root.append(node("h2", "", "Agent details"), node("p", "muted", "Select an agent or connection. Frequent collaborators cluster together; the map shows up to 500 strongest connections."));
    return;
  }
  root.append(node("span", "swarm-type", TYPES[agent.kind]), node("h2", "", agent.name),
    node("p", "", description(agent)), node("span", `swarm-state state-${agent.state}`, STATES[agent.state] || ""));
  if (agent.kind !== "operator") root.append(node("p", "muted", `${runtimeLabel(agent.agent_runtime)}${agent.model ? ` · ${agent.model}` : ""}`));
  if (agent.pending_approval_count > 0) {
    root.append(node("p", "", `${agent.pending_approval_count} pending Kern approval${agent.pending_approval_count === 1 ? "" : "s"}.`));
    const approvals = node("button", "ghost sm", "View approvals");
    approvals.dataset.action = "show-tab";
    approvals.dataset.tab = "approvals";
    root.append(approvals);
  }
  if (agent.next_run_at) root.append(node("p", "muted", `Next trigger: ${new Date(agent.next_run_at).toLocaleString()}`));
  const edges = interactions.filter(edge => edge.sender_thread_id === agent.thread_id || edge.target_thread_id === agent.thread_id);
  if (edges.length) {
    root.append(node("h3", "", "Strongest connections · last 7 UTC days"));
    for (const edge of edges.sort((a, b) => b.count - a.count)) {
      const outgoing = edge.sender_thread_id === agent.thread_id;
      const otherId = outgoing ? edge.target_thread_id : edge.sender_thread_id;
      const other = agentById.get(otherId);
      const link = node("button", "swarm-connection", `${outgoing ? "→" : "←"} ${other?.name || otherId} · ${edge.count}`);
      link.addEventListener("click", () => { selectedEdge = edge; renderEdges(); renderDetails(); });
      root.append(link);
    }
  }
  if (agent.kind === "operator") {
    root.append(node("p", "muted", "Counts include accepted operator messages. Automated triggers and agent replies are excluded from operator links."));
    return;
  }
  const open = node("button", "primary sm", "Open conversation");
  open.addEventListener("click", () => { void openAgent(agent); });
  root.append(open);
}
function renderAttention() {
  const root = $("swarm-attention");
  root.replaceChildren();
  const agents = snapshot.agents.filter(agent => agent.state === "failed" || agent.pending_approval_count > 0);
  $("swarm-attention-count").textContent = agents.length;
  if (!agents.length) root.append(node("p", "muted", "No errors or pending approvals."));
  for (const agent of agents) {
    const button = node("button", "swarm-attention-item");
    button.append(node("strong", "", agent.name), node("span", "", [agent.state === "failed" ? "Error" : "", agent.pending_approval_count ? `${agent.pending_approval_count} pending approval${agent.pending_approval_count === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ")));
    button.addEventListener("click", () => selectAgent(agent.thread_id, true));
    root.append(button);
  }
}
function renderEdges() {
  const root = $("swarm-edges");
  root.replaceChildren();
  const defs = svg("defs", {});
  const marker = svg("marker", { id: "swarm-arrow", viewBox: "0 0 10 10", refX: 9, refY: 5, markerWidth: 5, markerHeight: 5, orient: "auto-start-reverse" });
  marker.append(svg("path", { d: "M 0 0 L 10 5 L 0 10 z", fill: "#438d87" }));
  defs.append(marker); root.append(defs);
  for (const edge of interactions) {
    const source = layout.positions.get(edge.sender_thread_id);
    const target = layout.positions.get(edge.target_thread_id);
    if (!source || !target) continue;
    const dx = target.x - source.x, dy = target.y - source.y;
    const distance = Math.hypot(dx, dy) || 1;
    const ux = dx / distance, uy = dy / distance;
    const x1 = source.x + 110 + ux * 44, y1 = source.y + 44 + uy * 44;
    const x2 = target.x + 110 - ux * 44, y2 = target.y + 44 - uy * 44;
    const cx = (x1 + x2) / 2 - uy * 50, cy = (y1 + y2) / 2 + ux * 50;
    const isSelected = selectedEdge?.sender_thread_id === edge.sender_thread_id && selectedEdge?.target_thread_id === edge.target_thread_id;
    const touchesSelection = [edge.sender_thread_id, edge.target_thread_id].includes(selectedId);
    const muted = selectedEdge ? !isSelected : selectedId && !touchesSelection;
    const path = svg("path", { d: `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`,
      fill: "none", stroke: isSelected ? "#153f3c" : "#438d87", "stroke-width": Math.min(9, 1.5 + Math.log2(1 + edge.count)),
      "marker-end": "url(#swarm-arrow)", tabindex: 0, role: "button", class: "swarm-edge",
      "aria-label": `${agentById.get(edge.sender_thread_id)?.name} to ${agentById.get(edge.target_thread_id)?.name}: ${edge.count} messages`,
      opacity: muted ? .12 : Math.min(.85, .3 + Math.log2(1 + edge.count) * .075) });
    const title = svg("title", {}); title.textContent = path.getAttribute("aria-label"); path.append(title);
    const select = () => { selectedEdge = edge; selectedId = null; render(); };
    path.addEventListener("click", select);
    path.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
    root.append(path);
    if (!muted && (interactions.length <= 12 || isSelected || touchesSelection)) {
      const label = svg("text", { x: (x1 + 2 * cx + x2) / 4, y: (y1 + 2 * cy + y2) / 4,
        class: "swarm-edge-count", "text-anchor": "middle", "dominant-baseline": "middle", "aria-hidden": true });
      label.textContent = edge.count.toLocaleString();
      root.append(label);
    }
  }
}
function applyScale() {
  if (!layout) return;
  $("swarm-canvas").style.transform = `scale(${scale})`;
  $("swarm-stage").style.width = `${layout.width * scale}px`;
  $("swarm-stage").style.height = `${layout.height * scale}px`;
  $("swarm-zoom-level").textContent = `${Math.round(scale * 100)}%`;
}
function fitMap() {
  if (!layout) return;
  const viewport = $("swarm-viewport");
  scale = Math.min(1, (viewport.clientWidth - 24) / layout.width, (viewport.clientHeight - 24) / layout.height);
  applyScale(); viewport.scrollTo(0, 0);
}
function render() {
  if (!snapshot) return;
  if (!bound) {
    $("swarm-search").addEventListener("input", event => { search = event.target.value.toLowerCase().trim(); render(); });
    $("swarm-fit").addEventListener("click", fitMap);
    $("swarm-arrange").addEventListener("click", () => { layout = null; render(); fitMap(); });
    for (const [id, factor] of [["swarm-zoom-in", 1.25], ["swarm-zoom-out", .8]]) {
      $(id).addEventListener("click", () => { scale = Math.max(.1, Math.min(1.5, scale * factor)); applyScale(); });
    }
    $("swarm-actual").addEventListener("click", () => { scale = 1; applyScale(); });
    bound = true;
  }
  const agents = [...agentById.values()];
  const key = JSON.stringify([[...agentById.keys()].sort(), interactions.map(edge => `${edge.sender_thread_id}:${edge.target_thread_id}`).sort()]);
  if (key !== layoutKey || !layout) { layout = layoutAgents(agents, interactions); layoutKey = key; }
  $("swarm-canvas").style.width = `${layout.width}px`;
  $("swarm-canvas").style.height = `${layout.height}px`;
  const ids = new Set(agentById.keys());
  for (const [id, card] of cards) if (!ids.has(id)) { card.remove(); cards.delete(id); }
  for (const agent of agents) {
    let card = cards.get(agent.thread_id);
    if (!card) {
      card = node("button", "swarm-card"); card.dataset.threadId = agent.thread_id;
      const statusLine = node("span", "swarm-status-line");
      statusLine.append(node("span", "swarm-state"), node("span", "swarm-approval"));
      card.append(node("span", "swarm-avatar"), node("span", "swarm-type"), node("strong", "swarm-agent-name"), node("span", "swarm-agent-description"), statusLine);
      card.addEventListener("click", () => selectAgent(agent.thread_id));
      $("swarm-nodes").append(card); cards.set(agent.thread_id, card);
    }
    const position = layout.positions.get(agent.thread_id);
    card.style.left = `${position.x}px`; card.style.top = `${position.y}px`;
    const agentDescription = description(agent);
    const contentKey = JSON.stringify([agent.kind, agent.state, agent.pending_approval_count,
      agent.name, agentDescription, selectedId === agent.thread_id]);
    // Search and connection refreshes do not change character content. Avoid
    // replacing every label and invalidating the whole catalog's layout.
    if (card.dataset.contentKey !== contentKey) {
      card.className = `swarm-card kind-${agent.kind} pose-${agent.state}`;
      const avatar = card.querySelector(".swarm-avatar");
      const appearance = `${agent.state}:${agent.pending_approval_count > 0}`;
      if (avatar.dataset.appearance !== appearance) { avatar.innerHTML = character(agent); avatar.dataset.appearance = appearance; }
      card.setAttribute("aria-pressed", String(selectedId === agent.thread_id));
      card.querySelector(".swarm-type").textContent = TYPES[agent.kind];
      card.querySelector(".swarm-agent-name").textContent = agent.name;
      card.querySelector(".swarm-agent-description").textContent = agentDescription;
      const status = card.querySelector(".swarm-state"); status.textContent = STATES[agent.state] || ""; status.className = `swarm-state state-${agent.state}`;
      const approval = card.querySelector(".swarm-approval"); approval.textContent = agent.pending_approval_count ? `${agent.pending_approval_count} approval${agent.pending_approval_count === 1 ? "" : "s"}` : "";
      card.title = `${TYPES[agent.kind]} · ${agent.name}\n${agentDescription}\n${STATES[agent.state] || ""}`;
      card.dataset.contentKey = contentKey;
    }
    card.classList.toggle("is-dimmed", !matches(agent));
  }
  for (const filter of document.querySelectorAll("[data-swarm-filter]")) {
    const state = filter.dataset.swarmFilter;
    filter.querySelector("strong").textContent = snapshot.agents.filter(agent => state === "needs-human" ? agent.pending_approval_count > 0 : agent.state === state).length;
    filter.setAttribute("aria-pressed", String(activeFilter === state));
  }
  const visibleEdges = interactions.filter(edge => ids.has(edge.sender_thread_id) && ids.has(edge.target_thread_id));
  $("swarm-total").textContent = `${snapshot.agents.length} agents + you · ${visibleEdges.length} connections shown · last 7 UTC days`;
  $("swarm-empty").hidden = snapshot.agents.length > 0;
  renderEdges(); renderAttention(); renderDetails(); applyScale();
}
export function setSwarmFilter(state) {
  if (!["busy", "failed", "idle", "needs-human"].includes(state)) return;
  activeFilter = activeFilter === state ? "all" : state;
  render();
}
export async function refreshSwarm() {
  if (document.hidden || $("panel-swarm").hidden) return;
  if (loading) { refreshPending = true; return; }
  loading = true;
  try {
    // Communication is optional: a failed count request must not hide agents.
    const [next, counts] = await Promise.all([api("GET", "/v1/swarm"), api("GET", "/v1/swarm/interactions").catch(() => null)]);
    const first = !snapshot;
    const changed = JSON.stringify([snapshot?.agents, interactions]) !== JSON.stringify([next.agents, counts?.interactions || interactions]);
    snapshot = next;
    agentById = new Map([operator, ...next.agents].map(agent => [agent.thread_id, agent]));
    if (counts) interactions = counts.interactions;
    if (selectedEdge) selectedEdge = interactions.find(edge => edge.sender_thread_id === selectedEdge.sender_thread_id && edge.target_thread_id === selectedEdge.target_thread_id) || null;
    if (changed) render();
    if (first) fitMap();
    $("swarm-updated").textContent = `Updated ${time(snapshot.generated_at)}`;
    refreshError = counts ? "" : "Could not refresh communication counts. Showing agents with the last available connections.";
  } catch (error) {
    refreshError = snapshot ? `Could not refresh Swarm. Showing the snapshot from ${time(snapshot.generated_at)}.` : "Could not load Swarm. Try opening this page again.";
    throw error;
  } finally {
    loading = false; renderError();
    if (refreshPending) { refreshPending = false; queueMicrotask(() => { void refreshSwarm().catch(() => {}); }); }
  }
}
