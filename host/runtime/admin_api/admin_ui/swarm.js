import { api } from "./api.js";
import { $, runtimeLabel } from "./helpers.js";
import { layoutAgents, rankAgents, tierOf } from "./swarm_layout.js";

const TYPES = { "on-demand": "On-demand", app: "App", standing: "Standing", spawned: "Spawned", operator: "Human", host: "Host" };
const STATES = { busy: "Working", failed: "Error", idle: "Idle" };
// Link colour follows the sender: you, automated host deliveries, or agents.
const TONES = { operator: "operator", "kern-host": "host" };
// Minimap dots mirror the status colours used on the map.
const DOT_COLORS = { busy: "#45d6c4", failed: "#f0708a", approval: "#e8c268", operator: "#f4c26b", host: "#8ea3bd", idle: "#5b677a" };
const MIN_ZOOM = .08, MAX_ZOOM = 2.5;
let snapshot = null;
let agentById = new Map();
let interactions = [];
let metrics = null;
let scores = new Map();
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
// Screen position = world position × k + (x, y). The camera is unbounded, so
// the map can be dragged past every agent into open space.
const camera = { x: 0, y: 0, k: 1 };
let glideTimer = 0;
let minimapFrame = 0;
let minimapView = null;
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
const kernHost = { thread_id: "kern-host", name: "Kern host", kind: "host", state: "", pending_approval_count: 0 };
// Each agent type is its own robot; poses change only the eyes and props.
function eyes(pose, points, size = 3) {
  if (pose === "failed") return points.map(([x, y]) => `<path d="m${x - 2.5} ${y - 2.5} 5 5m0-5-5 5"/>`).join("");
  if (pose === "idle") return points.map(([x, y]) => `<path d="M${x - 3} ${y}q3 2.5 6 0"/>`).join("");
  return points.map(([x, y]) => `<ellipse cx="${x}" cy="${y}" rx="${size * .7}" ry="${size}"/>`).join("");
}
const BOTS = {
  // A retro monitor on a stand: Apps are screens you open.
  app: (pose, look) => `
    <path class="bot-line" d="M26 13 21 5m17 8 5-8"/><circle class="bot-accent" cx="21" cy="5" r="2.5"/><circle class="bot-accent" cx="43" cy="5" r="2.5"/>
    <rect class="bot-accent" x="29" y="41" width="6" height="6"/><rect class="bot-accent" x="19" y="46" width="26" height="7" rx="3.5"/>
    <rect class="bot-shell" x="11" y="12" width="42" height="31" rx="7"/>
    <rect class="bot-screen" x="15.5" y="16.5" width="33" height="22" rx="4"/>
    <circle class="bot-accent" cx="49" cy="40" r="1.4"/>
    <g class="bot-eyes">${look(pose, [[26, 26], [38, 26]])}</g>
    ${pose === "busy" ? '<g class="bot-busy bot-code"><path d="M21 33h7M31 33h5M39 33h4"/></g>' : ""}`,
  // A watchful owl with a clock belly: Standing agents wake on schedule.
  standing: (pose, look) => `
    <path class="bot-accent" d="m17 21-2-13 10 8zm30 0 2-13-10 8z"/>
    <path class="bot-line" d="M25 55v3m-3 0h6m11-3v3m-3 0h6"/>
    <ellipse class="bot-shell" cx="32" cy="35" rx="18" ry="20"/>
    <circle class="bot-screen" cx="25" cy="29" r="7.5"/><circle class="bot-screen" cx="39" cy="29" r="7.5"/>
    <g class="bot-eyes">${look(pose, [[25, 29], [39, 29]], 3.4)}</g>
    <path class="bot-accent" d="M29.5 36h5L32 40z"/>
    <circle class="bot-dial" cx="32" cy="47" r="5.5"/>
    <g class="${pose === "busy" ? "bot-busy bot-hands" : "bot-hands"}"><path d="M32 47v-3.5M32 47l2.5 1.5"/></g>`,
  // A small hovering drone: Spawned agents are helpers sent on errands.
  spawned: (pose, look) => `
    <path class="bot-line" d="M32 24v-7"/>
    <g class="${pose === "busy" ? "bot-busy bot-rotor" : "bot-rotor"}"><ellipse class="bot-accent" cx="32" cy="16" rx="14" ry="2.6"/></g>
    <circle class="bot-accent" cx="17" cy="38" r="3.5"/><circle class="bot-accent" cx="47" cy="38" r="3.5"/>
    <circle class="bot-shell" cx="32" cy="37" r="14"/>
    <rect class="bot-screen" x="21" y="32" width="22" height="10" rx="5"/>
    <g class="bot-eyes">${look(pose, [[27, 37], [37, 37]], 2.4)}</g>
    <path class="bot-jet" d="M28 54v4m8-4v4"/>`,
  // A capsule chat buddy with an antenna: On-demand agents talk with you.
  "on-demand": (pose, look) => `
    <path class="bot-line" d="M32 16V9"/><circle class="bot-accent" cx="32" cy="7" r="3.2"/>
    <rect class="bot-shell" x="22" y="43" width="20" height="13" rx="6"/><circle class="bot-accent" cx="32" cy="49.5" r="2.2"/>
    <circle class="bot-accent" cx="11" cy="30" r="3.6"/><circle class="bot-accent" cx="53" cy="30" r="3.6"/>
    <rect class="bot-shell" x="11" y="15" width="42" height="30" rx="15"/>
    <rect class="bot-screen" x="16.5" y="20.5" width="31" height="19" rx="9.5"/>
    <g class="bot-eyes">${look(pose, [[26, 29], [38, 29]])}</g>
    ${pose === "busy" ? '<g class="bot-busy bot-typing"><path class="bot-bubble" d="M44 2h14a4 4 0 0 1 4 4v5a4 4 0 0 1-4 4h-9l-4 3v-3h-1a4 4 0 0 1-4-4V6a4 4 0 0 1 4-4z"/><circle cx="47" cy="8.5" r="1.3"/><circle cx="51" cy="8.5" r="1.3"/><circle cx="55" cy="8.5" r="1.3"/></g>' : ""}`,
};
function character(agent) {
  if (agent.kind === "operator") return `<svg viewBox="0 0 64 64" aria-hidden="true"><circle class="operator-head" cx="32" cy="22" r="11"/><path class="operator-body" d="M12 58v-6a20 20 0 0 1 40 0v6z"/><path class="operator-smile" d="M27 24q5 5 10 0"/></svg>`;
  if (agent.kind === "host") return `<svg viewBox="0 0 64 64" aria-hidden="true"><g class="host-server"><rect x="15" y="8" width="34" height="48" rx="6"/><path d="M15 24h34M15 40h34"/></g><g class="host-lights"><circle cx="22" cy="16" r="2.5"/><circle cx="22" cy="32" r="2.5"/><circle cx="22" cy="48" r="2.5"/></g><path class="host-slots" d="M30 16h12M30 32h12M30 48h12"/></svg>`;
  const pose = agent.state === "busy" ? "busy" : agent.state === "failed" ? "failed" : agent.pending_approval_count > 0 ? "needs-human" : "idle";
  const variant = Number(agent.thread_id.split("-").pop()) % 3;
  return `<svg viewBox="0 0 64 64" aria-hidden="true"><g class="swarm-bot bot-${agent.kind in BOTS ? agent.kind : "on-demand"}">
    ${(BOTS[agent.kind] || BOTS["on-demand"])(pose, eyes)}
    ${pose === "failed" ? '<g class="bot-badge badge-failed"><circle cx="54" cy="10" r="7"/><text x="54" y="13.5" text-anchor="middle">!</text></g>' : ""}
    ${pose === "needs-human" ? '<g class="bot-badge badge-question"><circle cx="54" cy="10" r="7"/><text x="54" y="13.5" text-anchor="middle">?</text></g>' : ""}
    ${pose === "idle" && variant === 0 ? '<text class="bot-zzz" x="50" y="12">z</text>' : ""}
  </g></svg>`;
}

function description(agent) {
  if (agent.kind === "operator") return "Your messages to the swarm";
  if (agent.kind === "host") return "Automated messages to the swarm";
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
  if (visible) { $("swarm-close").focus({ preventScroll: true }); scheduleMinimap(); }
  else if (wasVisible) $(window.matchMedia("(max-width: 860px)").matches ? "mobile-nav-toggle" : "tab-swarm").focus({ preventScroll: true });
}
function selectAgent(id, focus = false) {
  selectedId = id;
  selectedEdge = null;
  render();
  if (focus) centerOn(id);
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
    root.append(node("h2", "", "Agent details"), node("p", "muted", "Select an agent or connection. You sit at the centre; Kern host beside you sends automated deliveries. Agents with more weekly involvement orbit closer and look larger, rings mark involvement tiers, and collaborators drift together. Arrange applies the latest ranking. Dense maps draw only the 500 strongest links."),
      node("p", "muted", "Drag anywhere to explore. Scroll to pan; pinch, Ctrl + scroll or + and − to zoom; 0 fits everything."));
    return;
  }
  root.append(node("span", "swarm-type", TYPES[agent.kind]), node("h2", "", agent.name),
    node("p", "", description(agent)), node("span", `swarm-state state-${agent.state}`, STATES[agent.state] || ""));
  const synthetic = ["operator", "host"].includes(agent.kind);
  if (!synthetic) root.append(node("p", "muted", `${runtimeLabel(agent.agent_runtime)}${agent.model ? ` · ${agent.model}` : ""}`));
  if (!synthetic) {
    root.append(node("h3", "", "Involvement · last 7 UTC days"));
    if (metrics) {
      const values = metrics[agent.thread_id];
      const count = value => (value || 0).toLocaleString();
      const score = scores.get(agent.thread_id) || 0;
      root.append(node("p", "", `Direct operator messages: ${count(values?.operator_messages)}`),
        node("p", "", `Other agents interacted with: ${count(values?.agent_peers)}`),
        node("p", "", `Tokens processed: ${values?.total_tokens == null ? "Unavailable" : count(values.total_tokens) + (values.tokens_partial ? " (partial)" : "")}`),
        node("p", "muted", `Current involvement score: ${(score * 100).toFixed(1)}/100 · ${tierOf(score)} tier. Operator messages 60%, agent connections 25%, tokens 15%; each uses diminishing returns and is scaled across this swarm. Arrange applies this ranking.`));
    } else root.append(node("p", "muted", "Ranking metrics unavailable. Arrange after they load to apply the hierarchy."));
  }
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
      link.addEventListener("click", () => { selectedEdge = edge; render(); });
      root.append(link);
    }
  }
  if (agent.kind === "operator") {
    root.append(node("p", "muted", "Counts include accepted operator messages. Automated triggers and agent replies are excluded from operator links."));
    return;
  }
  if (agent.kind === "host") {
    root.append(node("p", "muted", "Counts include accepted automated messages, including scheduled triggers, approval outcomes and restart notices. Rejected deliveries and Bash jobs are excluded. Host messages start counting after this upgrade and do not contribute to agent involvement scores."));
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
    const button = node("button", `swarm-attention-item${agent.state === "failed" ? " is-failed" : ""}`);
    button.append(node("strong", "", agent.name), node("span", "", [agent.state === "failed" ? "Error" : "", agent.pending_approval_count ? `${agent.pending_approval_count} pending approval${agent.pending_approval_count === 1 ? "" : "s"}` : ""].filter(Boolean).join(" · ")));
    button.addEventListener("click", () => selectAgent(agent.thread_id, true));
    root.append(button);
  }
}
function renderEdges() {
  const root = $("swarm-edges");
  // Refreshes rebuild every link; keep keyboard focus on the same one.
  const focusedEdge = root.contains(document.activeElement) ? document.activeElement.dataset.edge : null;
  root.replaceChildren();
  const defs = svg("defs", {});
  // Arrowheads keep the same size regardless of message volume.
  for (const [suffix, tone] of [["", "agent"], ["-operator", "operator"], ["-host", "host"], ["-selected", "selected"]]) {
    const marker = svg("marker", { id: `swarm-arrow${suffix}`, viewBox: "0 0 10 10", refX: 8, refY: 5, markerWidth: 12, markerHeight: 12, markerUnits: "userSpaceOnUse", orient: "auto" });
    marker.append(svg("path", { d: "M 0 1 L 9 5 L 0 9 L 2 5 z", class: `swarm-arrowhead tone-${tone}` }));
    defs.append(marker);
  }
  const glow = svg("radialGradient", { id: "swarm-glow" });
  glow.append(svg("stop", { offset: "0%", class: "swarm-glow-core" }), svg("stop", { offset: "100%", class: "swarm-glow-edge" }));
  defs.append(glow); root.append(defs);
  // Orbit rings name the involvement tiers around the operator.
  const { center, rings } = layout;
  const outer = rings.at(-1)?.radius || 400;
  root.append(svg("circle", { cx: center.x, cy: center.y, r: Math.min(outer, 1400), fill: "url(#swarm-glow)", class: "swarm-glow" }));
  for (const ring of rings) {
    root.append(svg("circle", { cx: center.x, cy: center.y, r: ring.radius, class: "swarm-ring" }));
    const label = svg("text", { x: center.x + ring.radius * Math.cos(ring.angle), y: center.y + ring.radius * Math.sin(ring.angle), class: "swarm-ring-label", "text-anchor": "middle", "dominant-baseline": "middle", "aria-hidden": true });
    label.textContent = ring.label.toUpperCase();
    root.append(label);
  }
  const focus = selectedEdge ? [selectedEdge.sender_thread_id, selectedEdge.target_thread_id] : selectedId ? [selectedId] : [];
  const flows = [], labels = [];
  for (const edge of interactions) {
    const source = layout.positions.get(edge.sender_thread_id);
    const target = layout.positions.get(edge.target_thread_id);
    if (!source || !target) continue;
    const dx = target.x - source.x, dy = target.y - source.y;
    const distance = Math.hypot(dx, dy) || 1;
    const ux = dx / distance, uy = dy / distance;
    const x1 = source.x + ux * (source.r + 6), y1 = source.y + uy * (source.r + 6);
    const x2 = target.x - ux * (target.r + 7), y2 = target.y - uy * (target.r + 7);
    // A gentle bend separates reciprocal links, each on its own side.
    const bend = Math.max(18, Math.min(90, distance * .14));
    const cx = (x1 + x2) / 2 - uy * bend, cy = (y1 + y2) / 2 + ux * bend;
    const d = `M${x1},${y1} Q${cx},${cy} ${x2},${y2}`;
    const isSelected = selectedEdge?.sender_thread_id === edge.sender_thread_id && selectedEdge?.target_thread_id === edge.target_thread_id;
    const touchesSelection = [edge.sender_thread_id, edge.target_thread_id].includes(selectedId);
    const muted = selectedEdge ? !isSelected : selectedId && !touchesSelection;
    const tone = isSelected ? "selected" : TONES[edge.sender_thread_id] || "agent";
    const width = Math.min(9, 1.5 + Math.log2(1 + edge.count));
    const path = svg("path", { d, fill: "none", "stroke-width": width,
      "marker-end": `url(#swarm-arrow${tone === "agent" ? "" : `-${tone}`})`, tabindex: 0, role: "button", "data-edge": `${edge.sender_thread_id} ${edge.target_thread_id}`,
      class: `swarm-edge tone-${tone}${muted ? " is-muted" : ""}${isSelected ? " is-selected" : ""}${touchesSelection ? " is-related" : ""}`,
      "aria-label": `${agentById.get(edge.sender_thread_id)?.name} to ${agentById.get(edge.target_thread_id)?.name}: ${edge.count} messages`,
      opacity: Math.min(.9, .35 + Math.log2(1 + edge.count) * .08) });
    const title = svg("title", {}); title.textContent = path.getAttribute("aria-label"); path.append(title);
    const select = () => { selectedEdge = edge; selectedId = null; render(); };
    path.addEventListener("click", select);
    path.addEventListener("keydown", event => { if (["Enter", " "].includes(event.key)) { event.preventDefault(); select(); } });
    root.append(path);
    if (path.dataset.edge === focusedEdge) path.focus({ preventScroll: true });
    // Moving pulses mark links whose agents are working right now.
    const live = [edge.sender_thread_id, edge.target_thread_id].some(id => agentById.get(id)?.state === "busy");
    if (!muted && (live || isSelected) && flows.length < 80) {
      // Busier links pulse faster. CSSOM keeps this compatible with the CSP.
      const flow = svg("path", { d, class: `swarm-edge-flow tone-${tone}`, "stroke-width": Math.max(2, width * .55), "aria-hidden": true });
      flow.style.animationDuration = `${Math.max(.9, 3 - Math.log2(1 + edge.count) * .35).toFixed(2)}s`;
      flows.push(flow);
    }
    if (!muted && (interactions.length <= 12 || isSelected || touchesSelection)) {
      const label = svg("text", { x: (x1 + 2 * cx + x2) / 4, y: (y1 + 2 * cy + y2) / 4,
        class: "swarm-edge-count", "text-anchor": "middle", "dominant-baseline": "middle", "aria-hidden": true });
      label.textContent = edge.count.toLocaleString();
      labels.push(label);
    }
  }
  root.append(...flows, ...labels);
  // Selecting an agent or link fades agents outside that neighbourhood.
  const related = new Set(focus);
  if (selectedId) for (const edge of interactions) {
    if (edge.sender_thread_id === selectedId) related.add(edge.target_thread_id);
    if (edge.target_thread_id === selectedId) related.add(edge.sender_thread_id);
  }
  for (const [id, card] of cards) card.classList.toggle("is-faded", related.size > 0 && !related.has(id));
}

function viewportSize() {
  const viewport = $("swarm-viewport");
  return [viewport.clientWidth, viewport.clientHeight];
}
function applyCamera() {
  const { x, y, k } = camera;
  $("swarm-canvas").style.transform = `translate(${x}px, ${y}px) scale(${k})`;
  // The dot grid moves with the camera; its spacing folds by powers of two
  // so it reads as endless open space at every zoom level.
  let spacing = 28 * k;
  while (spacing < 16) spacing *= 2;
  while (spacing > 56) spacing /= 2;
  const grid = $("swarm-grid");
  grid.style.backgroundSize = `${spacing}px ${spacing}px`;
  grid.style.backgroundPosition = `${x}px ${y}px`;
  $("swarm-viewport").classList.toggle("is-far", k < .4);
  $("swarm-zoom-level").textContent = `${Math.round(k * 100)}%`;
  scheduleMinimap();
}
// Button and keyboard moves glide; direct manipulation must track 1:1.
function stopGlide() {
  clearTimeout(glideTimer);
  for (const id of ["swarm-canvas", "swarm-grid"]) $(id).classList.remove("is-gliding");
}
function glide() {
  for (const id of ["swarm-canvas", "swarm-grid"]) $(id).classList.add("is-gliding");
  clearTimeout(glideTimer);
  glideTimer = setTimeout(stopGlide, 420);
}
function fitScale() {
  const [w, h] = viewportSize();
  return layout ? Math.max(.001, Math.min((w - 32) / layout.width, (h - 32) / layout.height)) : 1;
}
// Huge catalogs may need less than the usual minimum to fit; zooming out
// may always reach the fitted view.
const minZoom = () => Math.min(MIN_ZOOM, fitScale());
function zoomAt(screenX, screenY, factor, smooth = false) {
  const k = Math.max(minZoom(), Math.min(MAX_ZOOM, camera.k * factor));
  camera.x = screenX - (screenX - camera.x) * k / camera.k;
  camera.y = screenY - (screenY - camera.y) * k / camera.k;
  camera.k = k;
  smooth ? glide() : stopGlide();
  applyCamera();
}
function zoomCentered(factor) { const [w, h] = viewportSize(); zoomAt(w / 2, h / 2, factor, true); }
function lookAt(worldX, worldY, k = camera.k, smooth = true) {
  const [w, h] = viewportSize();
  Object.assign(camera, { k, x: w / 2 - worldX * k, y: h / 2 - worldY * k });
  smooth ? glide() : stopGlide();
  applyCamera();
}
function fitMap(smooth = false) {
  if (!layout) return;
  lookAt(layout.width / 2, layout.height / 2, Math.min(1, fitScale()), smooth);
}
// Large swarms open on the inner orbits at a readable size; the rest extends
// beyond the edges and stays reachable by dragging, the minimap or Fit all.
function initialView() {
  if (!layout) return;
  const [w, h] = viewportSize();
  if (Math.min((w - 32) / layout.width, (h - 32) / layout.height) >= .45) fitMap();
  else lookAt(layout.center.x, layout.center.y, .6, false);
}
function centerOn(id) {
  const point = layout?.positions.get(id);
  if (point) lookAt(point.x, point.y, Math.max(camera.k, .8));
}
function scheduleMinimap() {
  if (!minimapFrame) minimapFrame = requestAnimationFrame(() => { minimapFrame = 0; drawMinimap(); });
}
function drawMinimap() {
  const canvas = $("swarm-minimap");
  if (!layout || !canvas.offsetParent) return;
  const ratio = window.devicePixelRatio || 1, w = canvas.clientWidth, h = canvas.clientHeight;
  if (canvas.width !== Math.round(w * ratio) || canvas.height !== Math.round(h * ratio)) {
    canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
  }
  const context = canvas.getContext("2d");
  context.setTransform(ratio, 0, 0, ratio, 0, 0);
  context.clearRect(0, 0, w, h);
  // The minimap widens to include the view, so empty space never loses you.
  const [vw, vh] = viewportSize();
  const view = { left: -camera.x / camera.k, top: -camera.y / camera.k, right: (vw - camera.x) / camera.k, bottom: (vh - camera.y) / camera.k };
  const left = Math.min(0, view.left), top = Math.min(0, view.top);
  const right = Math.max(layout.width, view.right), bottom = Math.max(layout.height, view.bottom);
  const scale = Math.min((w - 12) / (right - left), (h - 12) / (bottom - top));
  const ox = (w - (right - left) * scale) / 2 - left * scale, oy = (h - (bottom - top) * scale) / 2 - top * scale;
  minimapView = { scale, ox, oy };
  context.strokeStyle = "rgba(148, 163, 184, .16)";
  for (const ring of layout.rings) {
    context.beginPath();
    context.arc(ox + layout.center.x * scale, oy + layout.center.y * scale, ring.radius * scale, 0, Math.PI * 2);
    context.stroke();
  }
  for (const [id, point] of layout.positions) {
    const agent = agentById.get(id);
    const tone = agent?.kind === "operator" ? "operator" : agent?.kind === "host" ? "host" : agent?.state === "failed" ? "failed"
      : agent?.pending_approval_count > 0 ? "approval" : agent?.state === "busy" ? "busy" : "idle";
    context.fillStyle = DOT_COLORS[tone];
    context.globalAlpha = agent && !matches(agent) ? .25 : 1;
    context.beginPath();
    context.arc(ox + point.x * scale, oy + point.y * scale, Math.max(1.5, point.r * scale), 0, Math.PI * 2);
    context.fill();
  }
  context.globalAlpha = 1;
  context.fillStyle = "rgba(69, 214, 196, .08)";
  context.strokeStyle = "rgba(230, 235, 242, .7)";
  const rect = [ox + view.left * scale, oy + view.top * scale, (view.right - view.left) * scale, (view.bottom - view.top) * scale];
  context.fillRect(...rect); context.strokeRect(...rect);
}
function bindMinimap() {
  const canvas = $("swarm-minimap");
  let dragging = null;
  const jump = event => {
    if (!minimapView) return;
    const box = canvas.getBoundingClientRect();
    lookAt((event.clientX - box.left - minimapView.ox) / minimapView.scale, (event.clientY - box.top - minimapView.oy) / minimapView.scale, camera.k, false);
  };
  canvas.addEventListener("pointerdown", event => { dragging = event.pointerId; canvas.setPointerCapture(event.pointerId); jump(event); event.preventDefault(); });
  canvas.addEventListener("pointermove", event => { if (dragging === event.pointerId) jump(event); });
  for (const name of ["pointerup", "pointercancel"]) canvas.addEventListener(name, () => { dragging = null; });
}
function bindMapPan() {
  const viewport = $("swarm-viewport");
  const pointers = new Map();
  let drag = null, pinch = null, suppressClick = false;
  const point = event => { const box = viewport.getBoundingClientRect(); return [event.clientX - box.left, event.clientY - box.top]; };
  viewport.addEventListener("pointerdown", event => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    suppressClick = false;
    pointers.set(event.pointerId, point(event));
    if (pointers.size === 2) {
      // Two fingers pinch around their midpoint; lift both to drag again.
      const [[ax, ay], [bx, by]] = [...pointers.values()];
      pinch = { distance: Math.hypot(ax - bx, ay - by) || 1, k: camera.k };
      drag = null; suppressClick = true;
      return;
    }
    // Drags may start on agents or links too; their click is kept unless the
    // pointer actually travels, so capture waits for real movement.
    drag = { id: event.pointerId, x: event.clientX, y: event.clientY, cameraX: camera.x, cameraY: camera.y, moved: false };
    // Pressing open map space (grid, canvas, rings) gives the map keyboard focus.
    if (!event.target.closest(".swarm-card, .swarm-edge")) viewport.focus({ preventScroll: true });
  });
  viewport.addEventListener("pointermove", event => {
    if (!pointers.has(event.pointerId)) return;
    pointers.set(event.pointerId, point(event));
    if (pinch && pointers.size === 2) {
      const [[ax, ay], [bx, by]] = [...pointers.values()];
      zoomAt((ax + bx) / 2, (ay + by) / 2, pinch.k * Math.hypot(ax - bx, ay - by) / pinch.distance / camera.k);
      return;
    }
    if (!drag || drag.id !== event.pointerId) return;
    const dx = event.clientX - drag.x, dy = event.clientY - drag.y;
    if (!drag.moved && Math.hypot(dx, dy) < 4) return;
    if (!drag.moved) {
      drag.moved = true;
      viewport.setPointerCapture(event.pointerId);
      viewport.classList.add("is-panning");
      stopGlide();
    }
    camera.x = drag.cameraX + dx; camera.y = drag.cameraY + dy;
    applyCamera();
  });
  const finish = event => {
    pointers.delete(event.pointerId);
    if (pointers.size < 2) pinch = null;
    if (drag?.id !== event.pointerId) return;
    if (drag.moved) suppressClick = true;
    drag = null;
    viewport.classList.remove("is-panning");
  };
  for (const name of ["pointerup", "pointercancel", "lostpointercapture"]) viewport.addEventListener(name, finish);
  // Capture waits for movement so clicks keep their target; a press released
  // outside the map before then must still end, or a stale pointer would turn
  // the next press into a pinch.
  for (const name of ["pointerup", "pointercancel"]) window.addEventListener(name, finish);
  viewport.addEventListener("click", event => {
    if (!suppressClick) return;
    suppressClick = false;
    event.stopPropagation(); event.preventDefault();
  }, true);
  viewport.addEventListener("wheel", event => {
    event.preventDefault();
    const unit = event.deltaMode === 1 ? 16 : event.deltaMode === 2 ? viewport.clientHeight : 1;
    if (event.ctrlKey || event.metaKey) {
      // Trackpad pinches arrive as Ctrl + wheel in every major browser.
      const [x, y] = point(event);
      zoomAt(x, y, Math.exp(-event.deltaY * unit * .0035));
    } else {
      camera.x -= (event.shiftKey && !event.deltaX ? event.deltaY : event.deltaX) * unit;
      camera.y -= (event.shiftKey && !event.deltaX ? 0 : event.deltaY) * unit;
      stopGlide(); applyCamera();
    }
  }, { passive: false });
  viewport.addEventListener("keydown", event => {
    if (event.altKey || event.ctrlKey || event.metaKey) return;
    const pan = { ArrowLeft: [1, 0], ArrowRight: [-1, 0], ArrowUp: [0, 1], ArrowDown: [0, -1] }[event.key];
    if (pan) { camera.x += pan[0] * 90; camera.y += pan[1] * 90; glide(); applyCamera(); }
    else if (["+", "="].includes(event.key)) zoomCentered(1.25);
    else if (["-", "_"].includes(event.key)) zoomCentered(.8);
    else if (event.key === "0") fitMap(true);
    else return;
    event.preventDefault();
  });
  // The camera owns positioning: undo browser scrolling from focus or find,
  // then glide to agents reached with the keyboard while they are off screen.
  viewport.addEventListener("scroll", () => { viewport.scrollTop = 0; viewport.scrollLeft = 0; });
  viewport.addEventListener("focusin", event => {
    const card = event.target.closest?.(".swarm-card");
    if (!card || drag) return;
    const view = viewport.getBoundingClientRect(), box = card.querySelector(".swarm-avatar").getBoundingClientRect();
    if (box.left < view.left || box.top < view.top || box.right > view.right || box.bottom > view.bottom) {
      const point = layout?.positions.get(card.dataset.threadId);
      if (point) lookAt(point.x, point.y);
    }
  });
  new ResizeObserver(scheduleMinimap).observe(viewport);
}
function render() {
  if (!snapshot) return;
  if (!bound) {
    bindMapPan();
    bindMinimap();
    for (const item of document.querySelectorAll("[data-legend-kind]")) {
      item.querySelector(".swarm-legend-icon").innerHTML = character({ kind: item.dataset.legendKind, thread_id: "legend-1", state: "idle", pending_approval_count: 0 });
    }
    $("swarm-search").addEventListener("input", event => { search = event.target.value.toLowerCase().trim(); render(); });
    $("swarm-fit").addEventListener("click", () => fitMap(true));
    $("swarm-arrange").addEventListener("click", () => { layout = null; render(); fitMap(true); });
    for (const [id, factor] of [["swarm-zoom-in", 1.25], ["swarm-zoom-out", .8]]) {
      $(id).addEventListener("click", () => zoomCentered(factor));
    }
    $("swarm-actual").addEventListener("click", () => zoomCentered(1 / camera.k));
    bound = true;
  }
  const agents = [...agentById.values()];
  const key = JSON.stringify([...agentById.keys()].sort());
  if (key !== layoutKey || !layout) { layout = layoutAgents(agents, interactions, metrics || {}); layoutKey = key; }
  $("swarm-canvas").style.width = `${layout.width}px`;
  $("swarm-canvas").style.height = `${layout.height}px`;
  const ids = new Set(agentById.keys());
  for (const [id, card] of cards) if (!ids.has(id)) { card.remove(); cards.delete(id); }
  for (const agent of agents) {
    let card = cards.get(agent.thread_id);
    if (!card) {
      card = node("button", "swarm-card"); card.dataset.threadId = agent.thread_id;
      const label = node("span", "swarm-label");
      const statusLine = node("span", "swarm-status-line");
      statusLine.append(node("span", "swarm-state"), node("span", "swarm-approval"));
      label.append(node("strong", "swarm-agent-name"), statusLine);
      const hover = node("span", "swarm-hovercard");
      hover.append(node("span", "swarm-type"), node("span", "swarm-agent-description"));
      card.append(node("span", "swarm-avatar"), label, hover);
      card.addEventListener("click", () => selectAgent(agent.thread_id));
      $("swarm-nodes").append(card); cards.set(agent.thread_id, card);
    }
    // Positions are orb centres; the orb size encodes involvement.
    const position = layout.positions.get(agent.thread_id);
    card.style.left = `${position.x}px`; card.style.top = `${position.y}px`;
    card.style.setProperty("--orb", `${position.r * 2}px`);
    const agentDescription = description(agent);
    const contentKey = JSON.stringify([agent.kind, agent.state, agent.pending_approval_count,
      agent.name, agentDescription, selectedId === agent.thread_id]);
    // Search and connection refreshes do not change character content. Avoid
    // replacing every label and invalidating the whole catalog's layout.
    if (card.dataset.contentKey !== contentKey) {
      card.className = `swarm-card kind-${agent.kind} pose-${agent.state}${agent.pending_approval_count > 0 ? " has-approval" : ""}`;
      const avatar = card.querySelector(".swarm-avatar");
      const appearance = `${agent.kind}:${agent.state}:${agent.pending_approval_count > 0}`;
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
  $("swarm-total").textContent = `${snapshot.agents.length} agents + you + Kern host · ${visibleEdges.length} connections shown · last 7 UTC days`;
  $("swarm-empty").hidden = snapshot.agents.length > 0;
  renderEdges(); renderAttention(); renderDetails(); applyCamera();
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
    const changed = JSON.stringify([snapshot?.agents, interactions, metrics]) !== JSON.stringify([next.agents, counts?.interactions || interactions, counts ? counts.metrics || null : metrics]);
    snapshot = next;
    agentById = new Map([operator, kernHost, ...next.agents].map(agent => [agent.thread_id, agent]));
    // A selected agent that left the roster must not keep the rest faded.
    if (selectedId && !agentById.has(selectedId)) selectedId = null;
    if (counts) { interactions = counts.interactions; metrics = counts.metrics || null; }
    scores = rankAgents([...agentById.values()], metrics || {});
    if (selectedEdge) selectedEdge = interactions.find(edge => edge.sender_thread_id === selectedEdge.sender_thread_id && edge.target_thread_id === selectedEdge.target_thread_id) || null;
    if (changed) render();
    if (first) initialView();
    $("swarm-updated").textContent = `Updated ${time(snapshot.generated_at)}`;
    refreshError = counts ? "" : "Could not refresh communication counts or ranking metrics. Showing agents with the last available connections and metrics.";
  } catch (error) {
    refreshError = snapshot ? `Could not refresh Swarm. Showing the snapshot from ${time(snapshot.generated_at)}.` : "Could not load Swarm. Try opening this page again.";
    throw error;
  } finally {
    loading = false; renderError();
    if (refreshPending) { refreshPending = false; queueMicrotask(() => { void refreshSwarm().catch(() => {}); }); }
  }
}
