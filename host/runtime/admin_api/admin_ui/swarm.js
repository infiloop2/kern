import { api } from "./api.js";
import { $, runtimeLabel } from "./helpers.js";
import { poseForAgent, SWARM_POSES } from "./swarm_pose.js";

const LABELS = { busy: "Busy", failed: "Failed", idle: "Idle", "needs-human": "Needs you" };
let snapshot = null;
let activeFilter = "all";
let search = "";
let selectedId = null;
let loading = false;
let searchTimer = null;
let refreshPending = false;
let bound = false;
let refreshError = "";
let navigationError = "";
const figures = new Map();
let shownMessageSeq = null;
let sceneTimer = null;
let captionIndex = 0;
const walks = new Map();
const bubbles = new Map();
const messageIcons = new Map();
const reducedMotion = window.matchMedia("(prefers-reduced-motion: reduce)");

function clearSceneMotion() {
  for (const animation of walks.values()) animation.cancel();
  walks.clear();
  for (const [bubble, timer] of bubbles) { clearTimeout(timer); bubble.remove(); }
  bubbles.clear();
  for (const [icon, timer] of messageIcons) { clearTimeout(timer); icon.remove(); }
  messageIcons.clear();
}
function onScreen(figure) {
  if (!figure || figure.hidden) return false;
  const rect = figure.getBoundingClientRect();
  return rect.width > 0 && rect.top > 150 && rect.bottom < innerHeight - 20;
}
function showBubble(agent, text) {
  const figure = figures.get(agent.thread_id);
  if (!text || !onScreen(figure)) return;
  for (const [old, timer] of bubbles) {
    if (old.dataset.threadId === agent.thread_id) { clearTimeout(timer); old.remove(); bubbles.delete(old); }
  }
  // Two brief task captions at most. Full text remains in agent details.
  if (bubbles.size >= 2) {
    const [old, timer] = bubbles.entries().next().value;
    clearTimeout(timer); old.remove(); bubbles.delete(old);
  }
  const bubble = node("div", "swarm-caption-bubble");
  bubble.dataset.threadId = agent.thread_id;
  bubble.append(node("strong", "", agent.name), node("span", "", text));
  $("swarm-bubbles").append(bubble);
  const rect = figure.getBoundingClientRect();
  const left = Math.max(12, Math.min(innerWidth - bubble.offsetWidth - 12, rect.x + rect.width / 2 - bubble.offsetWidth / 2));
  bubble.style.left = `${left}px`;
  bubble.style.top = `${Math.max(110, rect.y - bubble.offsetHeight - 8)}px`;
  bubbles.set(bubble, setTimeout(() => { bubble.remove(); bubbles.delete(bubble); }, 6500));
}
function showMessageIcon(source, target) {
  if (!onScreen(source) || !onScreen(target)) return;
  const from = source.getBoundingClientRect();
  const to = target.getBoundingClientRect();
  const icon = node("div", "swarm-message-icon");
  icon.setAttribute("aria-hidden", "true");
  icon.innerHTML = '<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M4 5.5h16v10H10l-4 3v-3H4z"/><circle cx="8" cy="10.5" r="1"/><circle cx="12" cy="10.5" r="1"/><circle cx="16" cy="10.5" r="1"/></svg>';
  icon.style.left = `${Math.max(12, Math.min(innerWidth - 44, (from.x + from.width / 2 + to.x + to.width / 2) / 2 - 17))}px`;
  icon.style.top = `${Math.max(110, Math.min(innerHeight - 44, (from.y + from.height / 2 + to.y + to.height / 2) / 2 - 17))}px`;
  $("swarm-bubbles").append(icon);
  messageIcons.set(icon, setTimeout(() => { icon.remove(); messageIcons.delete(icon); }, 2800));
}
function walk(figure, dx, dy, returning = true) {
  if (reducedMotion.matches || document.hidden || walks.size >= 2 || walks.has(figure) || !onScreen(figure)) return;
  const home = { transform: "translate(0, 0)" };
  const away = { transform: `translate(${dx}px, ${dy}px)` };
  const animation = figure.animate(returning ? [home, away, away, home] : [away, home], {
    duration: returning ? Math.min(3800, Math.max(1600, Math.hypot(dx, dy) * 12)) : 1100, easing: "ease-in-out",
  });
  walks.set(figure, animation);
  figure.classList.add("is-walking");
  const finish = () => { if (walks.get(figure) === animation) walks.delete(figure); if (!walks.has(figure)) figure.classList.remove("is-walking"); };
  animation.onfinish = finish;
  animation.oncancel = finish;
}
function sceneBeat() {
  if (!snapshot || document.hidden || $("panel-swarm").hidden) return;
  const agents = snapshot.agents.filter(agent => matches(agent) && onScreen(figures.get(agent.thread_id)));
  if (!agents.length) return;
  const titled = agents.filter(agent => agent.task || agent.purpose);
  if (titled.length) {
    const agent = titled[captionIndex % titled.length];
    showBubble(agent, agent.task || agent.purpose);
  }
  // Working agents take short walks inside their part of the commons.
  // This is decoration, not another inferred activity or state.
  const working = agents.filter(agent => poseForAgent(agent) === "busy" && agent.thread_id !== selectedId);
  if (working.length) {
    const figure = figures.get(working[captionIndex % working.length].thread_id);
    const rect = figure.getBoundingClientRect();
    const room = figure.closest(".swarm-zone").getBoundingClientRect();
    const dx = Math.min(180, Math.max(0, room.right - rect.right - 12))
      || -Math.min(180, Math.max(0, rect.left - room.left - 12));
    const dy = Math.min(90, Math.max(0, room.bottom - rect.bottom - 12));
    walk(figure, dx, dy);
  }
  captionIndex += 1;
}
export function setSwarmVisible(visible) {
  const wasVisible = document.body.classList.contains("swarm-open");
  document.body.classList.toggle("swarm-open", visible);
  document.querySelector(".topbar").inert = visible;
  $("sidebar").inert = visible;
  if (sceneTimer) clearInterval(sceneTimer);
  if (searchTimer) clearTimeout(searchTimer);
  searchTimer = null;
  sceneTimer = null;
  clearSceneMotion();
  if (visible) {
    sceneTimer = setInterval(sceneBeat, 4500);
    $("swarm-close").focus({ preventScroll: true });
  } else if (wasVisible) {
    const back = window.matchMedia("(max-width: 860px)").matches ? $("mobile-nav-toggle") : $("tab-swarm");
    back.focus({ preventScroll: true });
  }
}
window.addEventListener("resize", clearSceneMotion);
document.addEventListener("visibilitychange", clearSceneMotion);
reducedMotion.addEventListener("change", clearSceneMotion);

function node(tag, className = "", text = "") {
  const element = document.createElement(tag);
  element.className = className;
  element.textContent = text;
  return element;
}
function time(value) {
  const date = new Date(value);
  return Number.isNaN(date.valueOf()) ? "" : date.toLocaleTimeString([], { hour: "numeric", minute: "2-digit" });
}
function hash(id) {
  return [...id].reduce((value, char) => (value * 31 + char.charCodeAt(0)) >>> 0, 0);
}
function matches(agent) {
  return (activeFilter === "all" || poseForAgent(agent) === activeFilter)
    && `${agent.name} ${agent.task || ""} ${agent.purpose || ""}`.toLowerCase().includes(search);
}

// All paths are repository-authored. Names and AI text only enter textContent.
function character(agent) {
  const pose = poseForAgent(agent);
  const variant = hash(agent.thread_id) % 3;
  const eyes = pose === "idle"
    ? '<path d="M16 23q3 3 6 0m6 0q3 3 6 0"/>'
    : '<ellipse cx="19" cy="23" rx="2" ry="3"/><ellipse cx="31" cy="23" rx="2" ry="3"/>';
  const hair = [
    '<path d="M25 12V7m0 1q-7-7-9-1 3 5 9 1"/>',
    '<path d="M18 12q0-10 6-7 5 3 1 7"/>',
    '<path d="M19 11l-3-6m15 6 3-6"/><circle cx="16" cy="5" r="2"/><circle cx="34" cy="5" r="2"/>',
  ][variant];
  return `<svg viewBox="0 0 50 60" aria-hidden="true">
    <ellipse class="critter-shadow" cx="25" cy="54" rx="16" ry="3"/>
    <g class="critter-body">
      <g class="critter-hair">${hair}</g>
      <path class="critter-foot" d="M16 46v6h-4m22-6v6h4"/>
      <rect class="critter-suit" x="9" y="12" width="32" height="36" rx="14"/>
      <rect class="critter-face" x="12" y="17" width="26" height="17" rx="8"/>
      <g class="critter-eyes">${eyes}</g>
      <path class="critter-mouth" d="${(pose === "needs-human" || pose === "failed") ? 'M23 29q2-3 4 0' : 'M22 28q3 3 6 0'}"/>
      <circle class="critter-cheek" cx="15" cy="28" r="2"/><circle class="critter-cheek" cx="35" cy="28" r="2"/>
      <path class="critter-arm arm-left" d="M10 36 5 40"/>
      <g class="arm-right"><path class="critter-arm" d="${pose === "needs-human" ? 'M40 34q9-3 6-12' : 'M40 36 45 40'}"/></g>
      ${pose === "busy" ? '<g class="critter-laptop"><path d="M13 38h24l-2 11H15z"/><path d="M11 50h28"/><circle cx="25" cy="44" r="1.5"/></g>' : ''}
      ${pose === "needs-human" ? '<g class="critter-question"><circle cx="43" cy="12" r="7"/><text x="43" y="15" text-anchor="middle">?</text></g>' : ''}
      ${pose === "failed" ? '<g class="critter-failed"><circle cx="43" cy="12" r="7"/><text x="43" y="15" text-anchor="middle">!</text></g>' : ''}
      ${pose === "idle" && variant === 0 ? '<text class="critter-zzz" x="39" y="13">z</text>' : ''}
    </g></svg>`;
}

function renderError() {
  $("swarm-error").textContent = [refreshError, navigationError].filter(Boolean).join(" ");
  $("swarm-error").hidden = !refreshError && !navigationError;
}
async function openAgent(agent) {
  try {
    const opened = await window.KernHost.openWorkspace(agent.kind === "app" ? "apps" : "chat", agent.thread_id);
    if (opened === false) throw new Error("the thread is no longer available");
    navigationError = "";
  } catch (error) {
    navigationError = `Could not open ${agent.name}: ${error.message}`;
  }
  renderError();
}
function selectAgent(id) {
  selectedId = id;
  for (const [key, figure] of figures) figure.setAttribute("aria-pressed", String(key === id));
  renderDetails();
  const agent = snapshot?.agents.find(item => item.thread_id === id);
  if (agent) showBubble(agent, agent.task || agent.purpose);
}
function renderDetails() {
  const root = $("swarm-detail");
  root.replaceChildren();
  const agent = snapshot?.agents.find(item => item.thread_id === selectedId);
  root.classList.toggle("has-selection", Boolean(agent));
  if (!agent) {
    root.append(node("p", "muted", "Pick an agent to see its task and open its conversation."));
    return;
  }
  const identity = node("div", "swarm-detail-identity");
  identity.append(node("span", `swarm-state state-${poseForAgent(agent)}`, LABELS[poseForAgent(agent)]),
    node("h2", "", agent.name),
    node("p", "muted", `${agent.kind === "app" ? "App" : agent.kind === "schedule" ? "Schedule" : "Chat"} · ${runtimeLabel(agent.agent_runtime)}${agent.model ? ` · ${agent.model}` : ""}`));
  const task = node("div", "swarm-detail-task");
  task.append(node("span", "swarm-caption", "TASK"), node("p", "", agent.task || "No task title yet."));
  if (agent.purpose) task.append(node("p", "muted", agent.purpose));
  if (agent.state !== "busy") task.append(node("p", "muted", agent.needs_human == null
    ? "Human input: not assessed." : agent.needs_human ? "May need your input · assessed by Host AI." : "No human blocker identified by Host AI."));
  if (agent.next_run_at) task.append(node("p", "muted", `Next scheduled turn: ${new Date(agent.next_run_at).toLocaleString()}`));
  const open = node("button", "primary sm", "Open conversation");
  open.addEventListener("click", () => { void openAgent(agent); });
  const close = node("button", "swarm-detail-close", "×");
  close.setAttribute("aria-label", "Close agent details");
  close.addEventListener("click", () => selectAgent(null));
  root.append(identity, task, open, close);
}

function renderAgents() {
  clearSceneMotion();
  const visible = snapshot.agents.filter(matches);
  $("swarm-house").classList.toggle("is-focused", activeFilter !== "all" || Boolean(search));
  const existingIds = new Set(snapshot.agents.map(agent => agent.thread_id));
  const visibleIds = new Set(visible.map(agent => agent.thread_id));
  const positions = new Map([...figures].map(([id, figure]) => [id, figure.getBoundingClientRect()]));
  for (const [id, figure] of figures) {
    if (!existingIds.has(id)) { figure.remove(); figures.delete(id); }
    else figure.hidden = !visibleIds.has(id);
  }
  for (const agent of visible) {
    const pose = poseForAgent(agent);
    let figure = figures.get(agent.thread_id);
    if (!figure) {
      figure = node("button", "swarm-figure");
      figure.type = "button";
      figure.dataset.threadId = agent.thread_id;
      figure.addEventListener("click", () => selectAgent(agent.thread_id));
      figure.addEventListener("focus", () => {
        const current = snapshot.agents.find(item => item.thread_id === agent.thread_id);
        if (current) showBubble(current, current.task || current.purpose);
      });
      figure.style.setProperty("--delay", `${-(hash(agent.thread_id) % 50) / 10}s`);
      figures.set(agent.thread_id, figure);
    }
    if (figure.dataset.pose !== pose) {
      figure.innerHTML = character(agent);
      figure.dataset.pose = pose;
      const label = node("span", "swarm-agent-label");
      label.append(node("strong", "swarm-agent-name"), node("span", "swarm-agent-task"), node("span", "swarm-agent-purpose"));
      figure.append(label);
    }
    figure.className = `swarm-figure pose-${pose} kind-${agent.kind}`;
    figure.querySelector(".swarm-agent-name").textContent = agent.name;
    figure.querySelector(".swarm-agent-task").textContent = agent.task || (agent.state === "busy" ? "Working · task not available" : "No task title yet");
    const purpose = figure.querySelector(".swarm-agent-purpose");
    purpose.textContent = agent.purpose || "";
    purpose.hidden = !agent.purpose;
    figure.title = [agent.name, agent.task || LABELS[pose], agent.purpose].filter(Boolean).join(" · ");
    figure.setAttribute("aria-label", `${agent.name}, ${LABELS[pose]}. ${agent.task || "No task title yet"}. Show details.`);
    figure.setAttribute("aria-pressed", String(agent.thread_id === selectedId));
    figure.hidden = false;
    const destination = $(`swarm-agents-${pose}`);
    if (figure.parentElement !== destination) destination.append(figure);
  }
  for (const agent of visible) {
    const figure = figures.get(agent.thread_id);
    const previous = positions.get(agent.thread_id);
    const current = figure.getBoundingClientRect();
    if (previous?.width && (previous.x !== current.x || previous.y !== current.y)) {
      walk(figure, previous.x - current.x, previous.y - current.y, false);
    }
  }
  for (const pose of Object.values(SWARM_POSES)) {
    const count = snapshot.agents.filter(agent => poseForAgent(agent) === pose).length;
    const shown = visible.filter(agent => poseForAgent(agent) === pose).length;
    $(`swarm-zone-${pose}`).hidden = activeFilter !== "all" && activeFilter !== pose;
    $(`swarm-empty-${pose}`).hidden = shown > 0;
    $(`swarm-count-${pose}`).textContent = count;
    const filter = document.querySelector(`[data-swarm-filter="${pose}"]`);
    filter.querySelector("strong").textContent = count;
    filter.setAttribute("aria-pressed", String(activeFilter === pose));
  }
  $("swarm-total").textContent = snapshot.has_more
    ? `Showing ${snapshot.agents.length} ${search ? "matches" : "agents"} · ${search ? "refine your search" : "search to find more"}`
    : `${snapshot.agents.length} ${search ? "matching agents" : "agents at home"}`;
  renderDetails();
}

function animatePeerDeliveries() {
  const deliveries = snapshot.peer_deliveries || [];
  const unseen = shownMessageSeq === null ? deliveries
    : deliveries.filter(delivery => delivery.seq > shownMessageSeq);
  if (deliveries.length) shownMessageSeq = Math.max(shownMessageSeq || 0, deliveries[0].seq);
  for (const delivery of unseen.reverse()) {
    if (Date.now() - new Date(delivery.timestamp).valueOf() >= 30000) continue;
    for (const id of [delivery.sender_thread_id, delivery.target_thread_id]) {
      const figure = figures.get(id);
      if (!figure || figure.hidden) continue;
      figure.classList.remove("is-messaging");
      // A finite greeting only on a new recorded message, never every poll.
      void figure.offsetWidth;
      figure.classList.add("is-messaging");
    }
    const source = figures.get(delivery.sender_thread_id);
    const target = figures.get(delivery.target_thread_id);
    showMessageIcon(source, target);
    if (onScreen(source) && onScreen(target)) {
      const from = source.getBoundingClientRect();
      const to = target.getBoundingClientRect();
      walk(source, to.x - from.x + (to.x >= from.x ? -48 : 48), to.y - from.y);
    }
  }
}
function render() {
  if (!snapshot) return;
  if (!bound) {
    $("swarm-search").addEventListener("input", event => {
      search = event.target.value.toLowerCase().trim();
      renderAgents();
      if (searchTimer) clearTimeout(searchTimer);
      searchTimer = setTimeout(() => { searchTimer = null; void refreshSwarm().catch(() => {}); }, 300);
    });
    $("panel-swarm").addEventListener("scroll", clearSceneMotion, { passive: true });
    bound = true;
  }
  renderAgents();
  animatePeerDeliveries();
}
export function setSwarmFilter(pose) {
  if (!Object.values(SWARM_POSES).includes(pose)) return;
  activeFilter = activeFilter === pose ? "all" : pose;
  render();
}
export async function refreshSwarm() {
  if (document.hidden || $("panel-swarm").hidden) return;
  if (loading) { refreshPending = true; return; }
  loading = true;
  const requestedSearch = search;
  try {
    const [next, peerFeed] = await Promise.all([
      api("GET", `/v1/swarm${requestedSearch ? `?q=${encodeURIComponent(requestedSearch)}` : ""}`),
      api("GET", "/v1/swarm/peer-messages").catch(() => null),
    ]);
    next.peer_deliveries = peerFeed?.messages || snapshot?.peer_deliveries || [];
    if (requestedSearch !== search) { refreshPending = true; return; }
    const changed = JSON.stringify([snapshot?.agents, snapshot?.peer_deliveries, snapshot?.has_more])
      !== JSON.stringify([next.agents, next.peer_deliveries, next.has_more]);
    const previousTasks = new Map(snapshot?.agents.map(agent => [agent.thread_id, agent.task]) || []);
    const tasksChanged = snapshot ? next.agents.filter(agent => agent.task
      && previousTasks.get(agent.thread_id) !== agent.task) : [];
    snapshot = next;
    if (changed) render();
    for (const agent of tasksChanged.slice(0, 2)) showBubble(agent, agent.task);
    $("swarm-updated").textContent = `Updated ${time(snapshot.generated_at)}`;
    refreshError = "";
  } catch (error) {
    refreshError = snapshot ? `Could not refresh Swarm. Showing the snapshot from ${time(snapshot.generated_at)}.` : "Could not load Swarm. Try opening this page again.";
    throw error;
  } finally {
    loading = false;
    renderError();
    if (refreshPending) { refreshPending = false; queueMicrotask(() => { void refreshSwarm().catch(() => {}); }); }
  }
}
