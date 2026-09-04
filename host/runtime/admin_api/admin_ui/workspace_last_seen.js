// Backend-synced unread markers for Chat, scheduled agents, and Web Apps.

try { localStorage.removeItem("kern.workspace-last-seen.v2"); }
catch (_error) { /* Legacy browser state is no longer authoritative. */ }

function itemMarker(kind, item = {}) {
  const marker = {
    message_seq: Math.max(0, Number(item.latest_message_seq) || 0),
  };
  if (kind === "apps") marker.revision = Math.max(0, Number(item.revision) || 0);
  return marker;
}

function hasAdvanced(kind, current, item = {}) {
  if (kind === "apps") {
    return current.revision > (Number(item.seen_revision) || 0);
  }
  return current.message_seq > (Number(item.seen_message_seq) || 0);
}

export function createWorkspaceLastSeen({ api, currentTab, currentRoute, getItem, render }) {
  const pending = new Set();

  function hasChanges(kind, item) {
    return hasAdvanced(kind, itemMarker(kind, item), item);
  }

  function markSeen(kind, item) {
    if (!item || !["chat", "apps"].includes(kind) || document.visibilityState !== "visible") return;
    const id = kind === "chat" ? item.thread_id : item.app_id;
    const route = currentRoute();
    const expectedTab = kind === "chat" ? "workspace-chat" : "workspace-web-apps";
    if (!id || currentTab() !== expectedTab || route?.itemId !== id) return;
    const listed = getItem(kind, id) || {};
    const marker = itemMarker(kind, item);
    if (!hasAdvanced(kind, marker, listed) || pending.has(`${kind}:${id}`)) return;
    const path = kind === "chat"
      ? `/v1/workspace/chat/threads/${encodeURIComponent(id)}/seen`
      : `/v1/workspace/web-apps/apps/${encodeURIComponent(id)}/seen`;
    const key = `${kind}:${id}`;
    pending.add(key);
    void api("POST", path, marker).then(response => {
      const current = getItem(kind, id);
      if (!current) return;
      current.seen_message_seq = Math.max(
        Number(current.seen_message_seq) || 0,
        Number(response.seen?.message_seq) || 0,
      );
      if (kind === "apps") {
        current.seen_revision = Math.max(
          Number(current.seen_revision) || 0,
          Number(response.seen?.revision) || 0,
        );
      }
      render();
    }).catch(() => {
      // Keep the dot until a later rendered refresh can advance the marker.
    }).finally(() => pending.delete(key));
  }

  return { hasChanges, markSeen };
}
