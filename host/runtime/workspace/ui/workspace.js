(() => {
  if (window.KernWorkspaceGlobal) return;

  const root = window.KernWorkspaceRoots.global;
  const $ = id => root.getElementById(id);
  const api = (method, path, body) => window.KernHost.api(method, `/v1/workspace${path}`, body);
  // The one runtime whose schedule message is a path, not a prompt
  // (host/session_options.py, host/agent_scripts.py).
  const SCRIPT_RUNTIME = "script";
  const runtimeLabel = runtime => ({
    codex: "Codex", "codex-2": "Codex 2", "codex-3": "Codex 3", claude_code: "Claude Code",
    grok: "Grok", "grok-2": "Grok 2", hermes: "Hermes", script: "Script",
  })[runtime] || runtime;
  const SCRIPT_PATH_PLACEHOLDER = "/mnt/kern-agent/agent-home/scripts/nightly-backup.sh";
  const state = {
    resource: "memory",
    memoryScope: "swarm",
    deleted: false,
    items: [],
    next: null,
    selected: null,
    creating: false,
    sessionOptions: null,
    activeRuntimes: null,
    sequence: 0,
  };

  const isScheduleResource = () => state.resource === "scheduled-agents";

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

  async function open(resource, itemId = null, view = "active") {
    if (!new Set(["memory", "schedules", "scheduled-agents"]).has(resource)) return;
    if (resource === "schedules") resource = "scheduled-agents";
    state.resource = resource;
    if (resource === "memory") {
      state.memoryScope = itemId && /^(?:app|thread|schedule)-/.test(itemId)
        ? "individual"
        : "swarm";
    }
    state.deleted = view === "archived";
    state.selected = null;
    state.creating = false;
    $("global-title").textContent = resource === "memory"
      ? "Memory"
      : "Standing agents";
    $("global-intro").textContent = resource === "memory"
      ? "Swarm memory shared across agent threads."
      : "Persistent agents with optional triggers, and scheduled Bash jobs.";
    $("global-new").textContent = resource === "memory"
      ? "New page"
      : "New standing agent";
    $("memory-search-wrap").hidden = resource !== "memory";
    $("memory-scope-toggle").hidden = resource !== "memory";
    renderMemoryScope();
    hideForms();
    const openingRoute = window.location.hash;
    const loading = loadItems(false);
    const openingSequence = state.sequence;
    await loading;
    if (state.sequence !== openingSequence || window.location.hash !== openingRoute) return undefined;
    if (view === "new") {
      newItem();
      return true;
    }
    if (itemId !== null) {
      const selected = await selectItem(String(itemId), true);
      if (
        resource === "memory"
        && selected === false
        && /^(?:app|thread|schedule)-/.test(String(itemId))
      ) {
        newItem(String(itemId));
        return true;
      }
      return selected;
    }
    return true;
  }

  async function loadItems(
    append,
    search = state.resource === "memory" ? $("memory-search").value : "",
  ) {
    const sequence = ++state.sequence;
    status("Loading…");
    const base = state.resource === "memory" ? "/memory" : "/schedules";
    const params = new URLSearchParams({ limit: "50" });
    if (state.resource === "memory") params.set("scope", state.memoryScope);
    if (state.deleted) params.set("deleted", "true");
    if (append && state.next) {
      params.set(state.resource === "memory" ? "cursor" : "before", state.next);
    }
    let path = `${base}?${params}`;
    if (state.resource === "memory" && search.trim() && !state.deleted) {
      const searchParams = new URLSearchParams({ q: search.trim(), limit: "50" });
      searchParams.set("scope", state.memoryScope);
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

  function renderMemoryScope() {
    for (const button of root.querySelectorAll("button[data-memory-scope]")) {
      const active = button.dataset.memoryScope === state.memoryScope;
      button.classList.toggle("active", active);
      button.setAttribute("aria-pressed", String(active));
    }
    if (state.resource !== "memory") return;
    const individual = state.memoryScope === "individual";
    validateMemoryContent();
    $("global-intro").textContent = individual
      ? "Private memory owned by one app, chat, or schedule thread."
      : "Swarm memory shared across agent threads.";
    $("memory-page-id-help").textContent = individual
      ? "Use an app-, thread-, or schedule- page ID."
      : "Shared page IDs cannot start with app-, thread-, or schedule-.";
  }

  function setMemoryScope(scope) {
    if (!new Set(["swarm", "individual"]).has(scope) || scope === state.memoryScope) return;
    state.memoryScope = scope;
    state.deleted = false;
    state.selected = null;
    state.creating = false;
    hideForms();
    window.KernHost.navigateWorkspace("memory");
    renderMemoryScope();
    void loadItems(false, $("memory-search").value);
  }

  function renderList() {
    const list = $("global-list");
    list.replaceChildren();
    if (!state.items.length) {
      const empty = document.createElement("p");
      empty.className = "global-list-description";
      empty.textContent = state.deleted ? (isScheduleResource() ? "No archived agents." : "Nothing deleted.") : "Nothing here yet.";
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
        : `${cadenceLabel(item)} · ${runtimeLabel(item.agent_runtime)} / ${item.model} / ${item.effort}`;
      const meta = document.createElement("span");
      meta.className = "global-list-meta";
      meta.textContent = state.resource === "memory"
        ? `r${item.revision} · ${relativeTime(item.updated_at)}`
        : state.deleted
          ? `${isScheduleResource() ? "archived" : "deleted"} · ${relativeTime(item.updated_at)}`
          : `next ${relativeTime(item.next_run_at)}`;
      button.append(title, description, meta);
      list.append(button);
    }
    $("global-load-more").hidden = !state.next;
    $("global-archive-toggle").textContent = state.deleted ? "Back to active" : isScheduleResource() ? "Show archived" : "Show deleted";
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

  async function selectItem(id, propagateTransientError = false) {
    state.creating = false;
    const sequence = ++state.sequence;
    window.KernHost.navigateWorkspace(state.resource, String(id));
    const path = state.resource === "memory"
      ? `/memory/pages/${encodeURIComponent(id)}`
      : `/schedules/${encodeURIComponent(id)}`;
    status("Loading…");
    try {
      const response = await api("GET", path);
      if (sequence !== state.sequence) return;
      const selected = response[state.resource === "memory" ? "page" : "schedule"];
      state.selected = selected;
      renderList();
      if (state.resource === "memory") await renderMemory(sequence);
      else await renderSchedule(sequence);
      status("");
      return true;
    } catch (error) {
      if (sequence === state.sequence) status(error.message || "Could not load item", "error");
      if (propagateTransientError && error.status !== 404) throw error;
      return false;
    }
  }

  function cancelItem() {
    state.selected = null;
    state.creating = false;
    hideForms();
    renderList();
    window.KernHost.navigateWorkspace(state.resource);
  }

  function newItem(prefilledMemoryId = "") {
    state.selected = null;
    state.creating = true;
    window.KernHost.navigateWorkspace(
      state.resource,
      prefilledMemoryId || null,
    );
    $("global-empty").hidden = true;
    if (state.resource === "memory") {
      $("schedule-form").hidden = true;
      $("memory-form").hidden = false;
      $("memory-page-id").disabled = false;
      $("memory-page-id").value = prefilledMemoryId;
      $("memory-description").value = "";
      $("memory-content").value = "";
      validateMemoryContent();
      resizeTextarea("memory-content");
      $("memory-meta").textContent = state.memoryScope === "individual"
        ? "New individual memory page"
        : "New swarm memory page";
      $("memory-link-graph").hidden = state.memoryScope === "individual";
      $("memory-links").replaceChildren();
      $("memory-backlinks").replaceChildren();
      $("memory-history").replaceChildren();
      $("memory-delete").hidden = true;
      setFormDisabled($("memory-form"), false);
      (prefilledMemoryId ? $("memory-description") : $("memory-page-id")).focus();
    } else {
      $("memory-form").hidden = true;
      $("schedule-form").hidden = false;
      setFormDisabled($("schedule-form"), true);
      resetScheduleForm();
      $("schedule-meta").textContent = "New standing agent";
      $("schedule-history").replaceChildren();
      $("schedule-delete").hidden = true;
      void ensureSessionOptions().then(() => {
        if (!state.creating || !isScheduleResource()) return;
        syncSessionSelectors();
        setFormDisabled($("schedule-form"), false);
        $("schedule-name").focus();
      }).catch(error => {
        if (!state.creating || !isScheduleResource()) return;
        status(error.message || "Could not load schedule options", "error");
      });
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
    validateMemoryContent();
    resizeTextarea("memory-content");
    $("memory-meta").textContent = `Revision ${page.revision} · edited by ${page.updated_by} · ${relativeTime(page.updated_at)}`;
    $("memory-link-graph").hidden = state.memoryScope === "individual";
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
    const textarea = typeof id === "string" ? $(id) : id;
    textarea.style.height = "auto";
    const borders = textarea.offsetHeight - textarea.clientHeight;
    textarea.style.height = `${textarea.scrollHeight + borders}px`;
  }

  function validateMemoryContent() {
    const content = $("memory-content");
    const maximum = state.memoryScope === "individual" ? 20000 : 2000;
    // maxlength bounds UTF-16 storage; validity counts Unicode characters.
    content.maxLength = maximum * 2;
    content.setCustomValidity([...content.value].length > maximum
      ? `Content must be at most ${maximum} characters.`
      : "");
  }

  async function saveMemory(event) {
    event.preventDefault();
    validateMemoryContent();
    if (!$("memory-form").reportValidity()) return;
    const operationSequence = state.sequence;
    const operationRoute = window.location.hash;
    const pageId = $("memory-page-id").value.trim();
    const individual = /^(?:app|thread|schedule)-/.test(pageId);
    if (individual !== (state.memoryScope === "individual")) {
      status(
        state.memoryScope === "individual"
          ? "Individual page IDs must start with app-, thread-, or schedule-."
          : "Swarm page IDs cannot start with app-, thread-, or schedule-.",
        "error",
      );
      return;
    }
    const body = {
      description: $("memory-description").value.trim(),
      content: $("memory-content").value,
      expected_revision: state.creating ? 0 : state.selected.revision,
    };
    try {
      const response = await api("PUT", `/memory/pages/${encodeURIComponent(pageId)}`, body);
      if (
        operationSequence !== state.sequence
        || state.resource !== "memory"
        || window.location.hash !== operationRoute
      ) return;
      state.creating = false;
      state.selected = response.page;
      await loadItems(false, $("memory-search").value);
      if (window.location.hash !== operationRoute) return;
      await selectItem(pageId);
      status("Memory page saved", "success");
    } catch (error) {
      status(error.message || "Could not save memory page", "error");
    }
  }

  async function deleteMemory() {
    if (!state.selected || !confirm(`Delete memory page “${state.selected.page_id}”?`)) return;
    const operationSequence = state.sequence;
    const operationRoute = window.location.hash;
    try {
      await api("DELETE", `/memory/pages/${encodeURIComponent(state.selected.page_id)}?expected_revision=${state.selected.revision}`);
      if (
        operationSequence !== state.sequence
        || state.resource !== "memory"
        || window.location.hash !== operationRoute
      ) return;
      state.selected = null;
      hideForms();
      window.KernHost.navigateWorkspace("memory");
      await loadItems(false);
      status("Memory page moved to Deleted", "success");
    } catch (error) {
      status(error.message || "Could not delete memory page", "error");
    }
  }

  // Re-read every time the schedule form is rendered: connecting a provider
  // from Home must reach an already-mounted panel without a page reload. The
  // option matrix itself is static, so only activation can change.
  async function ensureSessionOptions() {
    const response = await api("GET", "/schedules/session-options");
    state.sessionOptions = response.session_options;
    state.activeRuntimes = Array.isArray(response.active_runtimes)
      ? response.active_runtimes
      : null;
  }

  function resetScheduleForm() {
    $("schedule-name").value = "";
    $("schedule-purpose").value = "";
    renderTriggers([]);
  }

  async function renderSchedule(sequence) {
    await ensureSessionOptions();
    if (sequence !== state.sequence) return;
    const schedule = state.selected;
    $("global-empty").hidden = true;
    $("memory-form").hidden = true;
    $("schedule-form").hidden = false;
    $("schedule-name").value = schedule.name;
    $("schedule-purpose").value = schedule.purpose || "";
    renderTriggers(schedule.triggers || []);
    syncSessionSelectors(schedule.agent_runtime, schedule.model, schedule.effort);
    const script = schedule.agent_runtime === SCRIPT_RUNTIME;
    $("global-title").textContent = script ? "Script schedule" : "Standing agent";
    $("global-intro").textContent = script
      ? "Recurring message executed by the time-bounded Bash runtime."
      : "Persistent agent with zero or more automatic messages at fixed UTC times.";
    $("schedule-meta").textContent = `${schedule.next_run_at ? "Next " + schedule.next_run_at.replace("T", " ").replace("Z", " UTC") : "No automatic messages"} · last delivery attempt ${relativeTime(schedule.last_run_at)}`;
    $("schedule-delete").hidden = schedule.deleted;
    setFormDisabled($("schedule-form"), schedule.deleted);
    for (const button of $("schedule-triggers").querySelectorAll("button")) {
      if (schedule.deleted) button.disabled = true;
    }
    if (schedule.deleted) $("schedule-add-trigger").disabled = true;
    const history = await api("GET", `/schedules/${schedule.id}/revisions?limit=10`);
    if (sequence !== state.sequence) return;
    renderHistory(
      $("schedule-history"),
      history.revisions || [],
      "schedule",
      false,
      history.next_before,
    );
  }

  // Kern runs the script runtime itself, so it is never an operator-connected
  // provider and is never gated. Unknown activation gates nothing. A saved
  // schedule keeps its old runtime visible after deactivation, but disabled.
  function scheduleRuntimeUnavailable(value, current) {
    if (value === SCRIPT_RUNTIME) return false;
    if (!Array.isArray(state.activeRuntimes)) return false;
    return !state.activeRuntimes.includes(value);
  }

  function syncSessionSelectors(runtime, model, effort) {
    if (!state.sessionOptions) return;
    const allRuntimes = Object.keys(state.sessionOptions);
    const runtimes = allRuntimes.filter(value => (
      !scheduleRuntimeUnavailable(value, runtime) || value === runtime
    ));
    const chosen = runtime
      || runtimes.find(value => !scheduleRuntimeUnavailable(value, null))
      || runtimes[0];
    fillSelect(
      $("schedule-runtime"),
      runtimes,
      chosen,
      value => scheduleRuntimeUnavailable(value, runtime),
    );
    const selectedRuntime = $("schedule-runtime").value;
    const models = Object.keys(state.sessionOptions[selectedRuntime] || {});
    fillSelect($("schedule-model"), models, model && models.includes(model) ? model : models[0]);
    const efforts = state.sessionOptions[selectedRuntime]?.[$("schedule-model").value] || [];
    fillSelect($("schedule-effort"), efforts, effort && efforts.includes(effort) ? effort : efforts[0]);
    syncMessageField();
  }

  // The script runtime reads the same field as a path to a bash script in the
  // agent home rather than as a prompt, so the form says which one it wants.
  function syncMessageField() {
    const script = $("schedule-runtime").value === SCRIPT_RUNTIME;
    for (const row of $("schedule-triggers").children) {
      row.querySelector("[data-prompt-label]").textContent = script ? "Script path" : "Prompt";
      const prompt = row.querySelector("textarea");
      prompt.placeholder = script ? SCRIPT_PATH_PLACEHOLDER : "What should this agent do at these times?";
      prompt.rows = script ? 2 : 4;
      resizeTextarea(prompt);
    }
  }

  const triggerWidths = new WeakMap();
  const triggerResizeObserver = typeof ResizeObserver === "function" ? new ResizeObserver(entries => {
    for (const entry of entries) {
      if (triggerWidths.get(entry.target) === entry.contentRect.width) continue;
      triggerWidths.set(entry.target, entry.contentRect.width);
      resizeTextarea(entry.target);
    }
  }) : null;

  const weekdays = ["mon", "tue", "wed", "thu", "fri", "sat", "sun"];

  function renderTriggers(triggers) {
    for (const prompt of $("schedule-triggers").querySelectorAll("textarea")) triggerResizeObserver?.unobserve(prompt);
    $("schedule-triggers").replaceChildren();
    triggers.forEach(addTrigger);
    syncTriggerCount();
  }

  function syncTriggerCount() {
    $("schedule-add-trigger").disabled = $("schedule-triggers").children.length >= 5;
  }

  function addTrigger(trigger = {type: "daily", times: ["09:00"], prompt: ""}) {
    if ($("schedule-triggers").children.length >= 5) return;
    const row = document.createElement("fieldset");
    row.className = "schedule-trigger";
    // Static markup only; all saved values are assigned as properties below.
    row.innerHTML = `<legend>Trigger</legend>
      <div class="form-grid">
        <label><span>Frequency</span><select data-trigger-type><option value="daily">Daily</option><option value="weekly">Weekly</option></select></label>
        <div class="wide trigger-days" role="group" aria-label="Weekdays"></div>
        <div class="wide"><span class="resource-meta" data-times-label>Times (UTC)</span><div class="trigger-times"></div><button type="button" class="ghost sm" data-add-time>Add time</button></div>
        <label class="wide"><span data-prompt-label>Prompt</span><textarea data-trigger-prompt rows="4" maxlength="12000" required></textarea></label>
      </div>
      <button type="button" class="ghost danger sm" data-remove-trigger>Remove trigger</button>`;
    const type = row.querySelector("[data-trigger-type]");
    type.value = trigger.type;
    const days = row.querySelector(".trigger-days");
    for (const day of weekdays) {
      const label = document.createElement("label");
      const input = document.createElement("input");
      input.type = "checkbox";
      input.value = day;
      input.checked = (trigger.days || ["mon"]).includes(day);
      label.append(input, day[0].toUpperCase() + day.slice(1));
      days.append(label);
    }
    const times = row.querySelector(".trigger-times");
    const addTimeButton = row.querySelector("[data-add-time]");
    function syncTimes() {
      addTimeButton.hidden = type.value === "weekly";
      row.querySelector("[data-times-label]").textContent = type.value === "weekly" ? "Time (UTC)" : "Times (UTC)";
      addTimeButton.disabled = times.children.length >= 24;
      for (const remove of times.querySelectorAll("button")) remove.hidden = times.children.length === 1;
    }
    function addTime(value = "09:00") {
      if (times.children.length >= 24) return;
      const item = document.createElement("div");
      item.className = "trigger-time";
      const input = document.createElement("input");
      input.type = "time";
      input.required = true;
      input.setAttribute("aria-label", "Time (UTC)");
      input.value = value;
      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ghost sm";
      remove.textContent = "Remove time";
      remove.addEventListener("click", () => { item.remove(); syncTimes(); });
      item.append(input, remove);
      times.append(item);
      syncTimes();
    }
    (trigger.type === "daily" ? trigger.times : [trigger.time]).forEach(addTime);
    days.hidden = type.value !== "weekly";
    type.addEventListener("change", () => {
      days.hidden = type.value !== "weekly";
      if (type.value === "weekly") while (times.children.length > 1) times.lastElementChild.remove();
      syncTimes();
    });
    addTimeButton.addEventListener("click", () => addTime());
    row.querySelector("textarea").value = trigger.prompt || "";
    row.querySelector("[data-remove-trigger]").addEventListener("click", () => { triggerResizeObserver?.unobserve(row.querySelector("textarea")); row.remove(); syncTriggerCount(); });
    $("schedule-triggers").append(row);
    const prompt = row.querySelector("textarea");
    prompt.addEventListener("input", () => resizeTextarea(prompt));
    triggerResizeObserver?.observe(prompt);
    syncTriggerCount();
    syncMessageField();
  }

  function readTriggers() {
    return Array.from($("schedule-triggers").children, row => {
      const type = row.querySelector("[data-trigger-type]").value;
      const prompt = row.querySelector("textarea").value.trim();
      const times = Array.from(row.querySelectorAll("input[type=time]"), input => input.value);
      if (type === "daily") return {type, times, prompt};
      return {type, days: Array.from(row.querySelectorAll("input[type=checkbox]:checked"), input => input.value), time: times[0], prompt};
    });
  }

  function fillSelect(select, values, selected, unavailable = null) {
    select.replaceChildren();
    for (const value of values) {
      const option = document.createElement("option");
      option.value = value;
      const label = select.id === "schedule-runtime"
        ? runtimeLabel(value)
        : value.replaceAll("_", " ");
      option.disabled = Boolean(unavailable && unavailable(value));
      option.textContent = option.disabled ? `${label} (not activated)` : label;
      option.selected = value === selected;
      select.append(option);
    }
  }

  function cadenceLabel(schedule) {
    if (!schedule.triggers?.length) return "No automatic messages";
    return schedule.triggers.map(trigger => trigger.type === "daily"
      ? `Daily ${trigger.times.join(", ")} UTC`
      : `${trigger.days.map(day => day[0].toUpperCase() + day.slice(1)).join(", ")} ${trigger.time} UTC`
    ).join(" · ");
  }

  function scheduleOperationIsCurrent(operationSequence, operationRoute) {
    return operationSequence === state.sequence
      && isScheduleResource()
      && window.location.hash === operationRoute;
  }

  async function saveSchedule(event) {
    event.preventDefault();
    const operationSequence = state.sequence;
    const operationRoute = window.location.hash;
    const body = {
      name: $("schedule-name").value.trim(),
      purpose: $("schedule-purpose").value.trim(),
      triggers: readTriggers(),
      agent_runtime: $("schedule-runtime").value,
      model: $("schedule-model").value,
      effort: $("schedule-effort").value,
    };
    try {
      let response;
      if (state.creating) response = await api("POST", "/schedules", body);
      else response = await api("PUT", `/schedules/${state.selected.id}`, {
        ...body,
        expected_revision: state.selected.revision,
      });
      if (!scheduleOperationIsCurrent(operationSequence, operationRoute)) return;
      state.creating = false;
      state.selected = response.schedule;
      if (response.schedule.thread_id) {
        try {
          await window.KernHost.refreshNavigation();
          if (!scheduleOperationIsCurrent(operationSequence, operationRoute)) return;
          await window.KernHost.openWorkspace("chat", response.schedule.thread_id);
        } catch (error) {
          if (!scheduleOperationIsCurrent(operationSequence, operationRoute)) return;
          status(
            `Agent saved, but Chat could not be opened: ${error.message || "unavailable"}`,
            "error",
          );
        }
        return;
      }
      await loadItems(false);
      if (window.location.hash !== operationRoute) return;
      await selectItem(String(response.schedule.id));
      status("Agent saved", "success");
    } catch (error) {
      status(error.message || "Could not save agent", "error");
    }
  }

  async function deleteSchedule() {
    if (!state.selected || !confirm(`Archive “${state.selected.name}” and stop its triggers?`)) return;
    const operationSequence = state.sequence;
    const operationRoute = window.location.hash;
    try {
      await api("DELETE", `/schedules/${state.selected.id}?expected_revision=${state.selected.revision}`);
      if (!scheduleOperationIsCurrent(operationSequence, operationRoute)) return;
      state.selected = null;
      hideForms();
      status("Agent archived", "success");
      try {
        await window.KernHost.refreshNavigation();
      } catch (error) {
        if (!scheduleOperationIsCurrent(operationSequence, operationRoute)) return;
        status(
          `Agent archived, but navigation could not refresh: ${error.message || "unavailable"}`,
          "error",
        );
        return;
      }
      if (!scheduleOperationIsCurrent(operationSequence, operationRoute)) return;
      window.KernHost.navigateWorkspace("scheduled-agents");
      await loadItems(false);
    } catch (error) {
      status(error.message || "Could not archive agent", "error");
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
        : `${revision.name} · ${cadenceLabel(revision)} · ${runtimeLabel(revision.agent_runtime)}/${revision.model}/${revision.effort}`;
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
    const operationRoute = window.location.hash;
    const selectedResource = state.resource;
    const base = resource === "memory"
      ? `/memory/pages/${encodeURIComponent(state.selected.page_id)}`
      : `/schedules/${state.selected.id}`;
    try {
      await api("POST", `${base}/revisions/${revision}/restore`, {
        expected_revision: state.selected.revision,
      });
      if (
        operationSequence !== state.sequence
        || state.resource !== selectedResource
        || window.location.hash !== operationRoute
      ) return;
      const id = selectedId();
      await loadItems(false);
      if (window.location.hash !== operationRoute) return;
      await selectItem(id);
      status("Revision restored", "success");
    } catch (error) {
      status(error.message || "Could not restore revision", "error");
    }
  }

  root.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target : null;
    if (!target) return;
    const item = target.closest("button[data-item-id]");
    if (item) void selectItem(item.dataset.itemId);
    const restore = target.closest("button[data-restore-resource]");
    if (restore) void restoreRevision(restore.dataset.restoreResource, Number(restore.dataset.restoreRevision));
    const history = target.closest("button[data-load-history]");
    if (history) void loadEarlierHistory(history);
  });
  $("global-new").addEventListener("click", () => newItem());
  root.addEventListener("click", event => {
    const target = event.target instanceof Element ? event.target.closest("button[data-memory-scope]") : null;
    if (target) setMemoryScope(target.dataset.memoryScope);
  });
  $("global-archive-toggle").addEventListener("click", () => {
    state.deleted = !state.deleted;
    state.selected = null;
    hideForms();
    window.KernHost.navigateWorkspace(state.resource);
    void loadItems(false);
  });
  $("global-load-more").addEventListener("click", () => void loadItems(true, $("memory-search").value));
  let searchTimer;
  $("memory-search").addEventListener("input", () => {
    clearTimeout(searchTimer);
    searchTimer = setTimeout(() => void loadItems(false, $("memory-search").value), 250);
  });
  const growingEditorIds = ["memory-content"];
  $("memory-content").addEventListener("input", validateMemoryContent);
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
  $("memory-cancel").addEventListener("click", cancelItem);
  $("schedule-form").addEventListener("submit", saveSchedule);
  $("schedule-delete").addEventListener("click", () => void deleteSchedule());
  $("schedule-cancel").addEventListener("click", cancelItem);
  $("schedule-add-trigger").addEventListener("click", () => addTrigger());
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
