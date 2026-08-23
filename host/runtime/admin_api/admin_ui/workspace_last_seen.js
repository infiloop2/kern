// Cross-tab unread markers for Chat threads and Web Apps.

const STORAGE_KEY = "kern.workspace-last-seen.v2";

function emptyState() {
  return {
    active: { chat: false, apps: false },
    archived: { chat: false, apps: false },
    chat: {},
    apps: {},
  };
}

function parseState(value) {
  if (!value) return emptyState();
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object") return emptyState();
    return {
      active: {
        chat: parsed.active?.chat === true,
        apps: parsed.active?.apps === true,
      },
      archived: {
        chat: parsed.archived?.chat === true,
        apps: parsed.archived?.apps === true,
      },
      chat: parsed.chat && typeof parsed.chat === "object" ? parsed.chat : {},
      apps: parsed.apps && typeof parsed.apps === "object" ? parsed.apps : {},
    };
  } catch (_error) {
    return emptyState();
  }
}

function loadState() {
  try { return parseState(localStorage.getItem(STORAGE_KEY)); }
  catch (_error) { return emptyState(); }
}

function itemMarker(kind, item) {
  // Agent reasoning and tool activity do not create unread dots; only the
  // latest conversation message (and an App revision) advances the marker.
  const marker = { activity: Math.max(0, Number(item.latest_message_seq) || 0) };
  if (kind === "apps") marker.revision = Math.max(0, Number(item.revision) || 0);
  return marker;
}

function itemId(kind, item) {
  return kind === "chat" ? item.thread_id : item.app_id;
}

function mergeMarkers(kind, left = {}, right = {}) {
  if (!left || typeof left !== "object") left = {};
  if (!right || typeof right !== "object") right = {};
  const merged = {
    activity: Math.max(Number(left.activity) || 0, Number(right.activity) || 0),
  };
  if (kind === "apps") {
    merged.revision = Math.max(Number(left.revision) || 0, Number(right.revision) || 0);
  }
  return merged;
}

function mergeState(left, right) {
  const merged = emptyState();
  for (const kind of ["chat", "apps"]) {
    merged.active[kind] = left.active[kind] || right.active[kind];
    merged.archived[kind] = left.archived[kind] || right.archived[kind];
    for (const id of new Set([...Object.keys(left[kind]), ...Object.keys(right[kind])])) {
      merged[kind][id] = mergeMarkers(kind, left[kind][id], right[kind][id]);
    }
  }
  return merged;
}

export function createWorkspaceLastSeen({ currentTab, currentRoute, render }) {
  let state = loadState();

  function save() {
    try {
      // Merge immediately before the whole-map write so another tab's newer
      // marker cannot knowingly be replaced by this tab's snapshot.
      state = mergeState(loadState(), state);
      localStorage.setItem(STORAGE_KEY, JSON.stringify(state));
    } catch (_error) { /* Markers remain valid for this page load. */ }
  }

  function initialize(kind, items, archived) {
    if (archived || state.active[kind]) return;
    for (const item of items) {
      const id = itemId(kind, item);
      state[kind][id] = mergeMarkers(kind, state[kind][id], itemMarker(kind, item));
    }
    state.active[kind] = true;
    save();
  }

  function initializeArchived(kind, items, archived) {
    if (!archived || state.archived[kind]) return;
    for (const item of items) {
      const id = itemId(kind, item);
      state[kind][id] = mergeMarkers(kind, state[kind][id], itemMarker(kind, item));
    }
    state.archived[kind] = true;
    save();
  }

  function hasChanges(kind, item, archived) {
    if (!(archived ? state.archived[kind] : state.active[kind])) return false;
    const seen = state[kind][itemId(kind, item)];
    if (!seen || typeof seen !== "object") return true;
    const current = itemMarker(kind, item);
    return current.activity > (Number(seen.activity) || 0)
      || (kind === "apps" && current.revision > (Number(seen.revision) || 0));
  }

  function markSeen(kind, item) {
    if (!item || !["chat", "apps"].includes(kind) || document.visibilityState !== "visible") return;
    const id = itemId(kind, item);
    const route = currentRoute();
    const expectedTab = kind === "chat" ? "workspace-chat" : "workspace-web-apps";
    if (!id || currentTab() !== expectedTab || route?.itemId !== id) return;
    const current = itemMarker(kind, item);
    const seen = state[kind][id] || {};
    const next = { activity: Math.max(current.activity, Number(seen.activity) || 0) };
    if (kind === "apps") {
      next.revision = Math.max(current.revision, Number(seen.revision) || 0);
    }
    if (JSON.stringify(next) === JSON.stringify(seen)) return;
    state[kind][id] = next;
    save();
    render();
  }

  window.addEventListener("storage", event => {
    if (event.key !== STORAGE_KEY) return;
    if (event.newValue === null) state = emptyState();
    else {
      const incoming = parseState(event.newValue);
      state = mergeState(state, incoming);
      if (JSON.stringify(state) !== JSON.stringify(incoming)) save();
    }
    render();
  });

  return { initialize, initializeArchived, hasChanges, markSeen };
}
