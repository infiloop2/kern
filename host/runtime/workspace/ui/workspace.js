(() => {
  if (window.KernWorkspaceGlobal) return;

  const root = window.KernWorkspaceRoots.global;
  const $ = id => root.getElementById(id);
  const api = (method, path, body) => window.KernHost.api(method, `/v1/workspace${path}`, body);
  const state = {
    resource: "memory",
    deleted: false,
    items: [],
    next: null,
    selected: null,
    creating: false,
    sessionOptions: null,
    sequence: 0,
  };

  function status(message = "", kind = "") {
    $("global-status").textContent = message;
    $("global-status").className = `global-status${kind ? ` ${kind}` : ""}`;
  }

  function relativeTime(value) {
    if (!value) return "never";
    const seconds = Math.round((Date.now() - Date.parse(value)) / 1000);
    if (!Number.isFinite(seconds)) return value;
    if (Math.abs(seconds) < 60) return "just now";
    const minutes = Math.round(seconds / 60);
    if (Math.abs(minutes) < 60) return `${Math.abs(minutes)}m ${minutes >= 0 ? "ago" : "from now"}`;
    const hours = Math.round(minutes / 60);
    if (Math.abs(hours) < 24) return `${Math.abs(hours)}h ${hours >= 0 ? "ago" : "from now"}`;
    const days = Math.round(hours / 24);
    return `${Math.abs(days)}d ${days >= 0 ? "ago" : "from now"}`;
  }

  async function open(resource) {
    if (!new Set(["memory", "schedules"]).has(resource)) return;
    state.resource = resource;
    state.deleted = false;
    state.selected = null;
    state.creating = false;
    $("global-title").textContent = resource === "memory" ? "Memory" : "Schedules";
    $("global-intro").textContent = resource === "memory"
      ? "Durable pages available to every agent thread."
      : "Recurring work, each run in a fresh independent agent thread.";
    $("global-new").textContent = resource === "memory" ? "New page" : "New schedule";
    $("memory-search-wrap").hidden = resource !== "memory";
    hideForms();
    await loadItems(false);
  }

  async function loadItems(
    append,
    search = state.resource === "memory" ? $("memory-search").value : "",
  ) {
    const sequence = ++state.sequence;
    status("Loading…");
    const base = state.resource === "memory" ? "/memory" : "/schedules";
    const params = new URLSearchParams({ limit: "50" });
    if (state.deleted) params.set("deleted", "true");
    if (append && state.next) {
      params.set(state.resource === "memory" ? "cursor" : "before", state.next);
    }
    let path = `${base}?${params}`;
    if (state.resource === "memory" && search.trim() && !state.deleted) {
      const searchParams = new URLSearchParams({ q: search.trim(), limit: "50" });
      if (append && state.next) searchParams.set("cursor", state.next);
      path = `/memory/search?${searchParams}`;
    }
    try {
      const response = await api("GET", path);
      if (sequence !== state.sequence) return;
      const incoming = response[state.resource === "memory" ? "pages" : "schedules"] || [];
      state.items = append ? [...state.items, ...incoming] : incoming;
      state.next = response.next_cursor || response.next_before || null;
      renderList();
      status("");
    } catch (error) {
      if (sequence === state.sequence) status(error.message || "Could not load Workspace data", "error");
    }
  }

  function renderList() {
    const list = $("global-list");
    list.replaceChildren();
    if (!state.items.length) {
      const empty = document.createElement("p");
      empty.className = "global-list-description";
      empty.textContent = state.deleted ? "Nothing deleted." : "Nothing here yet.";
      list.append(empty);
    }
    for (const item of state.items) {
      const id = state.resource === "memory" ? item.page_id : String(item.id);
      const button = document.createElement("button");
      button.className = `global-list-item${selectedId() === id ? " active" : ""}`;
      button.dataset.itemId = id;
      const title = document.createElement("span");
      title.className = "global-list-title";
      title.textContent = state.resource === "memory" ? item.page_id : item.name;
      const description = document.createElement("span");
      description.className = "global-list-description";
      description.textContent = state.resource === "memory"
        ? item.description
        : `${cadenceLabel(item)} · ${item.agent_runtime} / ${item.model} / ${item.effort}`;
      const meta = document.createElement("span");
      meta.className = "global-list-meta";
      meta.textContent = state.resource === "memory"
        ? `r${item.revision} · ${relativeTime(item.updated_at)}`
        : state.deleted
          ? `deleted · ${relativeTime(item.updated_at)}`
          : `next ${relativeTime(item.next_run_at)}`;
      button.append(title, description, meta);
      list.append(button);
    }
    $("global-load-more").hidden = !state.next;
    $("global-archive-toggle").textContent = state.deleted ? "Back to active" : "Show deleted";
  }

  function selectedId() {
    if (!state.selected) return null;
    return state.resource === "memory" ? state.selected.page_id : String(state.selected.id);
  }

  function hideForms() {
    $("memory-form").hidden = true;
    $("schedule-form").hidden = true;
    $("global-empty").hidden = false;
  }

  async function selectItem(id) {
    state.creating = false;
    const sequence = ++state.sequence;
    const path = state.resource === "memory"
      ? `/memory/pages/${encodeURIComponent(id)}`
      : `/schedules/${encodeURIComponent(id)}`;
    status("Loading…");
    try {
      const response = await api("GET", path);
      if (sequence !== state.sequence) return;
      state.selected = response[state.resource === "memory" ? "page" : "schedule"];
      renderList();
      if (state.resource === "memory") await renderMemory(sequence);
      else await renderSchedule(sequence);
      status("");
    } catch (error) {
      if (sequence === state.sequence) status(error.message || "Could not load item", "error");
    }
  }

  function newItem() {
    state.selected = null;
    state.creating = true;
    $("global-empty").hidden = true;
    if (state.resource === "memory") {
      $("schedule-form").hidden = true;
      $("memory-form").hidden = false;
      $("memory-page-id").disabled = false;
      $("memory-page-id").value = "";
      $("memory-description").value = "";
      $("memory-content").value = "";
      resizeTextarea("memory-content");
      $("memory-meta").textContent = "New global memory page";
      $("memory-links").replaceChildren();
      $("memory-backlinks").replaceChildren();
      $("memory-history").replaceChildren();
      $("memory-delete").hidden = true;
      setFormDisabled($("memory-form"), false);
      $("memory-page-id").focus();
    } else {
      $("memory-form").hidden = true;
      $("schedule-form").hidden = false;
      setFormDisabled($("schedule-form"), false);
      resetScheduleForm();
      $("schedule-meta").textContent = "New global schedule";
      $("schedule-history").replaceChildren();
      $("schedule-runs").replaceChildren();
      $("schedule-delete").hidden = true;
      void ensureSessionOptions().then(() => syncSessionSelectors());
      $("schedule-name").focus();
    }
    renderList();
  }

  async function renderMemory(sequence) {
    const page = state.selected;
    $("global-empty").hidden = true;
    $("schedule-form").hidden = true;
    $("memory-form").hidden = false;
    $("memory-page-id").value = page.page_id;
    $("memory-description").value = page.description;
    $("memory-content").value = page.content;
    resizeTextarea("memory-content");
    $("memory-meta").textContent = `Revision ${page.revision} · edited by ${page.updated_by} · ${relativeTime(page.updated_at)}`;
    renderChips($("memory-links"), page.links || []);
    renderChips($("memory-backlinks"), page.backlinks || []);
    $("memory-delete").hidden = page.deleted;
    setFormDisabled($("memory-form"), page.deleted);
    // Page identifiers are stable even while an active page is editable.
    $("memory-page-id").disabled = true;
    const response = await api("GET", `/memory/pages/${encodeURIComponent(page.page_id)}/revisions?limit=40`);
    if (sequence !== state.sequence) return;
    renderHistory(
      $("memory-history"),
      response.revisions || [],
      "memory",
      false,
      response.next_before,
    );
  }

  function renderChips(container, values) {
    container.replaceChildren();
    if (!values.length) {
      const empty = document.createElement("span");
      empty.className = "global-list-description";
      empty.textContent = "None";
      container.append(empty);
      return;
    }
    for (const value of values) {
      const chip = document.createElement("span");
      chip.className = "chip";
      chip.textContent = value;
      container.append(chip);
    }
  }

  function setFormDisabled(form, disabled) {
    for (const field of form.querySelectorAll("input, textarea, select")) field.disabled = disabled;
    const save = form.querySelector('button[type="submit"]');
    if (save) save.disabled = disabled;
  }

  function resizeTextarea(id) {
    const textarea = $(id);
    textarea.style.height = "auto";
    const borders = textarea.offsetHeight - textarea.clientHeight;
    textarea.style.height = `${textarea.scrollHeight + borders}px`;
  }

  async function saveMemory(event) {
    event.preventDefault();
    const operationSequence = state.sequence;
    const pageId = $("memory-page-id").value.trim();
    const body = {
      description: $("memory-description").value.trim(),
      content: $("memory-content").value,
      expected_revision: state.creating ? 0 : state.selected.revision,
    };
    try {
      const response = await api("PUT", `/memory/pages/${encodeURIComponent(pageId)}`, body);
      if (operationSequence !== state.sequence || state.resource !== "memory") return;
      state.creating = false;
      state.selected = response.page;
      await loadItems(false, $("memory-search").value);
      await selectItem(pageId);
      status("Memory page saved", "success");
    } catch (error) {
      status(error.message || "Could not save memory page", "error");
    }
  }

  async function deleteMemory() {
    if (!state.selected || !confirm(`Delete memory page “${state.selected.page_id}”?`)) return;
    const operationSequence = state.sequence;
    try {
      await api("DELETE", `/memory/pages/${encodeURIComponent(state.selected.page_id)}?expected_revision=${state.selected.revision}`);
      if (operationSequence !== state.sequence || state.resource !== "memory") return;
      state.selected = null;
      hideForms();
      await loadItems(false);
      status("Memory page moved to Deleted", "success");
    } catch (error) {
      status(error.message || "Could not delete memory page", "error");
    }
  }

  async function ensureSessionOptions() {
    if (state.sessionOptions) return;
    const response = await api("GET", "/schedules/session-options");
    state.sessionOptions = response.session_options;
  }

  function resetScheduleForm() {
    $("schedule-name").value = "";
    $("schedule-message").value = "";
    resizeTextarea("schedule-message");
    $("schedule-cadence").value = "interval";
    $("schedule-interval").value = "60";
    $("schedule-time").value = "09:00";
    syncCadence();
  }

  async function renderSchedule(sequence) {
    await ensureSessionOptions();
    if (sequence !== state.sequence) return;
    const schedule = state.selected;
    $("global-empty").hidden = true;
    $("memory-form").hidden = true;
    $("schedule-form").hidden = false;
    $("schedule-name").value = schedule.name;
    $("schedule-message").value = schedule.message;
    resizeTextarea("schedule-message");
    syncSessionSelectors(schedule.agent_runtime, schedule.model, schedule.effort);
    $("schedule-cadence").value = schedule.cadence;
    $("schedule-interval").value = schedule.interval_minutes || 60;
    $("schedule-time").value = schedule.daily_time || "09:00";
    syncCadence();
    $("schedule-meta").textContent = `Schedule ${schedule.id} · revision ${schedule.revision} · next ${relativeTime(schedule.next_run_at)} · last run ${relativeTime(schedule.last_run_at)}`;
    $("schedule-delete").hidden = schedule.deleted;
    setFormDisabled($("schedule-form"), schedule.deleted);
    const [history, runs] = await Promise.all([
      api("GET", `/schedules/${schedule.id}/revisions?limit=10`),
      api("GET", `/schedules/${schedule.id}/runs?limit=40`),
    ]);
    if (sequence !== state.sequence) return;
    renderHistory(
      $("schedule-history"),
      history.revisions || [],
      "schedule",
      false,
      history.next_before,
    );
    renderRuns(runs.runs || [], false, runs.next_before);
  }

  function syncSessionSelectors(runtime, model, effort) {
    if (!state.sessionOptions) return;
    const runtimes = Object.keys(state.sessionOptions);
    fillSelect($("schedule-runtime"), runtimes, runtime || runtimes[0]);
    const selectedRuntime = $("schedule-runtime").value;
    const models = Object.keys(state.sessionOptions[selectedRuntime] || {});
    fillSelect($("schedule-model"), models, model && models.includes(model) ? model : models[0]);
    const efforts = state.sessionOptions[selectedRuntime]?.[$("schedule-model").value] || [];
    fillSelect($("schedule-effort"), efforts, effort && efforts.includes(effort) ? effort : efforts[0]);
  }

  function fillSelect(select, values, selected) {
    select.replaceChildren();
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      option.textContent = value.replaceAll("_", " ");
      option.selected = value === selected;
      select.append(option);
    }
  }

  function syncCadence() {
    const daily = $("schedule-cadence").value === "daily";
    $("schedule-interval-wrap").hidden = daily;
    $("schedule-time-wrap").hidden = !daily;
  }

  function cadenceLabel(schedule) {
    if (schedule.cadence === "daily") return `Daily ${schedule.daily_time} UTC`;
    return `Every ${schedule.interval_minutes} minutes`;
  }

  async function saveSchedule(event) {
    event.preventDefault();
    const operationSequence = state.sequence;
    const body = {
      name: $("schedule-name").value.trim(),
      message: $("schedule-message").value.trim(),
      cadence: $("schedule-cadence").value,
      agent_runtime: $("schedule-runtime").value,
      model: $("schedule-model").value,
      effort: $("schedule-effort").value,
    };
    if (body.cadence === "daily") body.daily_time = $("schedule-time").value;
    else body.interval_minutes = Number($("schedule-interval").value);
    try {
      let response;
      if (state.creating) response = await api("POST", "/schedules", body);
      else response = await api("PUT", `/schedules/${state.selected.id}`, {
        ...body,
        expected_revision: state.selected.revision,
      });
      if (operationSequence !== state.sequence || state.resource !== "schedules") return;
      state.creating = false;
      state.selected = response.schedule;
      await loadItems(false);
      await selectItem(String(response.schedule.id));
      status("Schedule saved", "success");
    } catch (error) {
      status(error.message || "Could not save schedule", "error");
    }
  }

  async function deleteSchedule() {
    if (!state.selected || !confirm(`Delete schedule “${state.selected.name}”?`)) return;
    const operationSequence = state.sequence;
    try {
      await api("DELETE", `/schedules/${state.selected.id}?expected_revision=${state.selected.revision}`);
      if (operationSequence !== state.sequence || state.resource !== "schedules") return;
      state.selected = null;
      hideForms();
      await loadItems(false);
      status("Schedule moved to Deleted", "success");
    } catch (error) {
      status(error.message || "Could not delete schedule", "error");
    }
  }

  function renderHistory(container, revisions, resource, append = false, nextBefore = null) {
    container.querySelector("[data-load-history]")?.remove();
    if (!append) container.replaceChildren();
    for (const revision of revisions) {
      const row = document.createElement("div");
      row.className = "history-row";
      const head = document.createElement("div");
      head.className = "history-row-head";
      const label = document.createElement("strong");
      label.textContent = `Revision ${revision.revision}${revision.deleted ? " · deleted" : ""}`;
      const restore = document.createElement("button");
      restore.className = "ghost sm";
      restore.type = "button";
      restore.dataset.restoreResource = resource;
      restore.dataset.restoreRevision = String(revision.revision);
      restore.textContent = "Restore";
      const copy = document.createElement("div");
      copy.className = "history-copy";
      copy.textContent = resource === "memory"
        ? `${revision.description}\n${revision.content}`
        : `${revision.name} · ${cadenceLabel(revision)} · ${revision.agent_runtime}/${revision.model}/${revision.effort}`;
      head.append(label, restore);
      row.append(head, copy);
      container.append(row);
    }
    if (nextBefore) {
      const more = document.createElement("button");
      more.className = "ghost sm";
      more.type = "button";
      more.dataset.loadHistory = resource;
      more.dataset.before = String(nextBefore);
      more.textContent = "Load earlier history";
      container.append(more);
    }
  }

  async function loadEarlierHistory(button) {
    if (!state.selected) return;
    const itemId = selectedId();
    const resource = button.dataset.loadHistory;
    const base = resource === "memory"
      ? `/memory/pages/${encodeURIComponent(state.selected.page_id)}`
      : `/schedules/${state.selected.id}`;
    button.disabled = true;
    try {
      const response = await api(
        "GET", `${base}/revisions?limit=${resource === "memory" ? 40 : 10}&before=${button.dataset.before}`,
      );
      if (selectedId() !== itemId) return;
      renderHistory(
        resource === "memory" ? $("memory-history") : $("schedule-history"),
        response.revisions || [],
        resource,
        true,
        response.next_before,
      );
    } catch (error) {
      button.disabled = false;
      status(error.message || "Could not load earlier history", "error");
    }
  }

  async function restoreRevision(resource, revision) {
    if (!state.selected || !confirm(`Restore revision ${revision}?`)) return;
    const operationSequence = state.sequence;
    const selectedResource = resource === "memory" ? "memory" : "schedules";
    const base = resource === "memory"
      ? `/memory/pages/${encodeURIComponent(state.selected.page_id)}`
      : `/schedules/${state.selected.id}`;
    try {
      await api("POST", `${base}/revisions/${revision}/restore`, {
        expected_revision: state.selected.revision,
      });
      if (operationSequence !== state.sequence || state.resource !== selectedResource) return;
      const id = selectedId();
      await loadItems(false);
      await selectItem(id);
      status("Revision restored", "success");
    } catch (error) {
      status(error.message || "Could not restore revision", "error");
    }
  }

  function renderRuns(runs, append = false, nextBefore = null) {
    const container = $("schedule-runs");
    container.querySelector("[data-load-runs]")?.remove();
    if (!append) container.replaceChildren();
    if (!runs.length && !append) {
      const empty = document.createElement("span");
      empty.className = "global-list-description";
      empty.textContent = "No runs yet.";
      container.append(empty);
      return;
    }
    for (const run of runs) {
      const row = document.createElement("div");
      row.className = "run-row";
      const head = document.createElement("div");
      head.className = "history-row-head";
      const title = document.createElement("strong");
      title.textContent = run.thread_id;
      const badge = document.createElement("span");
      badge.className = `run-status ${run.status}`;
      badge.textContent = run.status;
      const events = document.createElement("button");
      events.className = "ghost sm";
      events.type = "button";
      events.dataset.runEvents = String(run.id);
      events.textContent = "Messages";
      head.append(title, badge, events);
      const copy = document.createElement("div");
      copy.className = "run-copy";
      copy.textContent = `${run.agent_runtime}/${run.model}/${run.effort} · ${relativeTime(run.scheduled_for)}${run.error_message ? `\n${run.error_message}` : ""}`;
      const eventList = document.createElement("div");
      eventList.className = "run-events";
      eventList.dataset.runEventList = String(run.id);
      eventList.hidden = true;
      row.append(head, copy, eventList);
      container.append(row);
    }
    if (nextBefore) {
      const more = document.createElement("button");
      more.className = "ghost sm";
      more.type = "button";
      more.dataset.loadRuns = "true";
      more.dataset.before = String(nextBefore);
      more.textContent = "Load earlier runs";
      container.append(more);
    }
  }

  async function loadEarlierRuns(button) {
    if (!state.selected) return;
    const scheduleId = state.selected.id;
    button.disabled = true;
    try {
      const response = await api(
        "GET",
        `/schedules/${state.selected.id}/runs?limit=40&before=${button.dataset.before}`,
      );
      if (state.resource !== "schedules" || state.selected?.id !== scheduleId) return;
      renderRuns(response.runs || [], true, response.next_before);
    } catch (error) {
      button.disabled = false;
      status(error.message || "Could not load earlier runs", "error");
    }
  }

  async function toggleRunEvents(runId) {
    const container = root.querySelector(`[data-run-event-list="${CSS.escape(String(runId))}"]`);
    if (!container) return;
    if (!container.hidden) {
      container.hidden = true;
      return;
    }
    container.hidden = false;
    container.textContent = "Loading…";
    const scheduleId = state.selected?.id;
    try {
      const response = await api("GET", `/schedules/${scheduleId}/runs/${runId}/events`);
      if (state.resource !== "schedules" || state.selected?.id !== scheduleId) return;
      container.replaceChildren();
      if (!response.events.length) {
        container.textContent = response.retained ? "No messages." : "Conversation is no longer retained.";
        return;
      }
      appendRunEventMessages(container, response.events);
      addEarlierRunEventsButton(container, runId, response.events);
    } catch (error) {
      container.textContent = error.message || "Could not load run messages.";
    }
  }

  function appendRunEventMessages(container, events, before = null) {
    const fragment = document.createDocumentFragment();
    for (const event of events) {
      const message = document.createElement("div");
      const source = event.payload?.source || "agent";
      message.className = `run-message ${source}`;
      message.dataset.eventSeq = String(event.seq);
      message.textContent = event.payload?.message || event.payload?.error_message || "Run stopped.";
      fragment.append(message);
    }
    if (before) before.after(fragment);
    else container.append(fragment);
  }

  function addEarlierRunEventsButton(container, runId, events) {
    if (events.length < 20) return;
    const oldest = Math.min(...events.map(event => Number(event.seq)).filter(Number.isFinite));
    if (!Number.isFinite(oldest)) return;
    const button = document.createElement("button");
    button.className = "ghost sm run-earlier";
    button.type = "button";
    button.dataset.runEarlier = String(runId);
    button.dataset.before = String(oldest);
    button.textContent = "Load earlier messages";
    container.prepend(button);
  }

  async function loadEarlierRunEvents(button) {
    if (!state.selected) return;
    const scheduleId = state.selected.id;
    const runId = Number(button.dataset.runEarlier);
    const before = Number(button.dataset.before);
    button.disabled = true;
    try {
      const response = await api(
        "GET",
        `/schedules/${scheduleId}/runs/${runId}/events?before=${before}`,
      );
      if (state.resource !== "schedules" || state.selected?.id !== scheduleId) return;
      if (!response.events.length) {
        button.remove();
        return;
      }
      appendRunEventMessages(button.parentElement, response.events, button);
      const oldest = Math.min(...response.events.map(event => Number(event.seq)).filter(Number.isFinite));
      if (response.events.length < 20 || !Number.isFinite(oldest)) button.remove();
      else {
        button.dataset.before = String(oldest);
        button.disabled = false;
      }
    } catch (error) {
      button.disabled = false;
      status(error.message || "Could not load earlier run messages", "error");
    }
  }

  root.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const item = target.closest("button[data-item-id]");
    if (item) void selectItem(item.dataset.itemId);
    const restore = target.closest("button[data-restore-resource]");
    if (restore) void restoreRevision(restore.dataset.restoreResource, Number(restore.dataset.restoreRevision));
    const run = target.closest("button[data-run-events]");
    if (run) void toggleRunEvents(Number(run.dataset.runEvents));
    const earlier = target.closest("button[data-run-earlier]");
    if (earlier) void loadEarlierRunEvents(earlier);
    const history = target.closest("button[data-load-history]");
    if (history) void loadEarlierHistory(history);
    const runs = target.closest("button[data-load-runs]");
    if (runs) void loadEarlierRuns(runs);
  });
  $("global-new").addEventListener("click", newItem);
  $("global-archive-toggle").addEventListener("click", () => {
    state.deleted = !state.deleted;
    state.selected = null;
    hideForms();
    void loadItems(false);
  });
  $("global-load-more").addEventListener("click", () => void loadItems(true, $("memory-search").value));
  let searchTimer;
  $("memory-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => void loadItems(false, $("memory-search").value), 250);
  });
  const growingEditorIds = ["memory-content", "schedule-message"];
  for (const id of growingEditorIds) $(id).addEventListener("input", () => resizeTextarea(id));
  if (typeof ResizeObserver === "function") {
    const observedWidths = new WeakMap();
    const editorResizeObserver = new ResizeObserver(entries => {
      for (const entry of entries) {
        const width = entry.contentRect.width;
        if (observedWidths.get(entry.target) === width) continue;
        observedWidths.set(entry.target, width);
        resizeTextarea(entry.target.id);
      }
    });
    for (const id of growingEditorIds) editorResizeObserver.observe($(id));
  } else {
    window.addEventListener("resize", () => {
      for (const id of growingEditorIds) resizeTextarea(id);
    });
  }
  $("memory-form").addEventListener("submit", saveMemory);
  $("memory-delete").addEventListener("click", () => void deleteMemory());
  $("memory-cancel").addEventListener("click", hideForms);
  $("schedule-form").addEventListener("submit", saveSchedule);
  $("schedule-delete").addEventListener("click", () => void deleteSchedule());
  $("schedule-cancel").addEventListener("click", hideForms);
  $("schedule-cadence").addEventListener("change", syncCadence);
  $("schedule-runtime").addEventListener("change", () => syncSessionSelectors($("schedule-runtime").value));
  $("schedule-model").addEventListener("change", () => {
    const efforts = state.sessionOptions?.[$("schedule-runtime").value]?.[$("schedule-model").value] || [];
    fillSelect($("schedule-effort"), efforts, efforts[0]);
  });

  window.KernWorkspaceGlobal = {
    get resource() { return state.resource; },
    open,
  };
})();
