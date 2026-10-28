(() => {
"use strict";
// One panel for Chats (including spawned/Standing agents) and Apps.
window.KernReminders = function(root, button) {
  let target = null, generation = 0, rows = [], pending = [], nextBefore = null;
  let loading = false, lastLoad = 0, requestNumber = 0;
  const dialog = document.createElement("dialog");
  dialog.className = "reminders-dialog";
  dialog.setAttribute("aria-label", "Reminders");
  dialog.innerHTML = `<div class="reminders-heading"><h2>Reminders</h2><button type="button" class="ghost" data-close aria-label="Close reminders">Close</button></div>
    <p data-error role="status" hidden></p>
    <h3>Pending</h3><div data-pending></div>
    <h3>Completed</h3><div data-history></div>
    <button type="button" class="ghost" data-more hidden>Load more</button>`;
  root.append(dialog);
  const $ = selector => dialog.querySelector(selector);
  function error(text = "") { $("[data-error]").textContent = text; $("[data-error]").hidden = !text; }
  function path(id = target) { return `/v1/workspace/reminders/${encodeURIComponent(id)}`; }
  function render() {
    $("[data-pending]").replaceChildren();
    $("[data-history]").replaceChildren();
    for (const row of pending.concat(rows.filter(row => row.status !== "pending"))) {
      const card = document.createElement("article");
      card.className = "reminder-row";
      const time = document.createElement("div");
      time.className = "reminder-time";
      time.textContent = `${new Date(row.due_at).toLocaleString()} · ${row.status}`;
      const content = document.createElement("p");
      content.textContent = row.message;
      card.append(time, content);
      if (row.error) { const note = document.createElement("p"); note.textContent = row.error; card.append(note); }
      $(row.status === "pending" ? "[data-pending]" : "[data-history]").append(card);
    }
    if (!pending.length) $("[data-pending]").textContent = "No pending reminders.";
    if (!rows.some(row => row.status !== "pending")) $("[data-history]").textContent = "No completed reminders.";
    $("[data-more]").hidden = nextBefore === null;
  }
  async function refresh(more = false) {
    if (!target) return;
    const version = generation, id = target, request = ++requestNumber;
    loading = true;
    try {
      const result = await window.KernHost.api("GET", path(id) + (more ? `?before=${nextBefore}` : ""));
      if (version !== generation || request !== requestNumber) return;
      // Pending rows may finish while an older page is being requested.
      // Read their current records before replacing the complete pending list.
      const finished = more ? pending.filter(row => !result.pending.some(item => item.id === row.id)) : [];
      const refreshed = await Promise.all(finished.map(async row => {
        const known = result.reminders.find(item => item.id === row.id);
        if (known) return known;
        const page = await window.KernHost.api("GET", path(id) + `?before=${BigInt(row.id) + 1n}`);
        return page.reminders.find(item => item.id === row.id);
      }));
      if (version !== generation || request !== requestNumber) return;
      const previous = more ? rows.filter(row => !finished.some(item => item.id === row.id)) : [];
      rows = [...new Map(previous.concat(result.reminders, refreshed.filter(Boolean)).map(row => [row.id, row])).values()];
      rows.sort((a, b) => b.id - a.id);
      pending = result.pending;
      nextBefore = result.next_before;
      button.querySelector("[data-reminder-count]").textContent = result.pending_count ? String(result.pending_count) : "";
      button.setAttribute("aria-label", `Reminders, ${result.pending_count} pending`);
      lastLoad = Date.now();
      error(); render();
    } catch (e) { if (version === generation) error(e.message); }
    finally { if (version === generation && request === requestNumber) loading = false; }
  }
  button.addEventListener("click", () => { dialog.showModal(); void refresh(); });
  $("[data-close]").addEventListener("click", () => dialog.close());
  dialog.addEventListener("close", () => button.focus());
  $("[data-more]").addEventListener("click", () => void refresh(true));
  return {
    setTarget(id) {
      if (target !== id) {
        generation++; target = id; rows = []; pending = []; nextBefore = null;
        loading = false; lastLoad = 0;
        dialog.close(); error(); render();
        button.querySelector("[data-reminder-count]").textContent = "";
        button.setAttribute("aria-label", "Reminders");
      }
      button.hidden = !id;
      if (id && !loading && Date.now() - lastLoad > 10000) void refresh();
    },
  };
};
})();
