const pending = new Map();
let nextRequestId = 1;
let threads = [];
// The selected thread's live status ("idle" | "running"), from the host
// through the thread index. Turns and their contents render purely from the
// event stream.
let selectedThreadStatus = "idle";
// The selected thread opens on its newest event page. Live events page
// forward from the newest cursor; earlier history is prepended on demand from
// the oldest cursor. EVENTS_PAGE mirrors the app backend page size.
let threadEvents = [];
let threadEventsOldestSeq = null;
let threadEventsNewestSeq = 0;
let threadEventsInitialized = false;
let hasOlderThreadEvents = false;
let loadingOlderThreadEvents = false;
let lastChatScrollTop = 0;
let restoredChatScrollTop = null;
// Keep each opened thread's loaded window and reading position for the
// lifetime of this app frame. A browser reload intentionally starts fresh.
const threadViewStates = new Map();
const EVENTS_PAGE = 6;
const INITIAL_EVENT_PAGES = 3;
const VIEW_STATE_LIMIT = 50;
const ACTIVE_REFRESH_MS = 1000;
const IDLE_REFRESH_MS = 5000;
const APP_API_TIMEOUT_MS = 30000;
// The outer browser-to-app proxy can wait 50s for a synchronous provider
// acknowledgement. Sends and Stop must outlive that hop or a retry can
// duplicate a message the host accepted after the frame gave up.
const AGENT_DELIVERY_TIMEOUT_MS = 60 * 1000;
let selectedThreadId = null;
let selectedThreadName = null;
let selectedThreadRuntime = null;
let selectedThreadModel = null;
let selectedThreadEffort = null;
let selectedThreadArchived = false;
let showingArchivedThreads = false;
let showingActivity = true;
let sessionOptions = {};
let pendingAttachments = [];
let attachmentActivity = null;
let sendingMessage = false;
const ATTACHMENT_LIMIT = 10;
const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
const ACTIVITY_PRESENTATION = Object.freeze({
  reasoning: { icon: "✦", label: "Reasoning", detail: "Thought process", output: "Result" },
  plan: { icon: "✓", label: "Plan", detail: "Plan", output: "Result" },
  command: { icon: "›_", label: "Command", detail: "Context", output: "Terminal output" },
  file_change: { icon: "Δ", label: "File change", detail: "Changes", output: "Result" },
  tool: { icon: "◇", label: "Tool", detail: "Input", output: "Tool output" },
  agent: { icon: "◎", label: "Sub-agent", detail: "Assignment", output: "Result" },
  search: { icon: "⌕", label: "Search", detail: "Query", output: "Results" },
  image: { icon: "▧", label: "Image", detail: "Image details", output: "Output" },
  wait: { icon: "◷", label: "Wait", detail: "Wait details", output: "Result" },
  status: { icon: "•", label: "Status", detail: "Details", output: "Output" },
});
// Render guards: polling re-renders only when data actually
// changed, so a draft or the reading scroll position survives
// refreshes that bring nothing new.
let renderedThreadsKey = null;
let renderedHistoryKey = null;
let renderedHistoryThread = null;
// Per-entry rendered HTML, so a poll only patches history that actually
// changed (most often one streaming activity snapshot).
const renderedEntryHtml = new Map();
let forceScrollBottom = false;
let statusOwner = null;

const $ = id => document.getElementById(id);
const runtimeLabel = runtime => runtime === "claude_code" ? "Claude Code" : runtime === "codex" ? "Codex" : runtime === "hermes" ? "Hermes" : runtime;
const optionLabel = value => value.split(/[-_]/).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
// Claude Code model ids carry the provider prefix ("claude-opus-5"); the
// runtime name already says Claude Code, so the pill reads "Opus 5".
const modelLabel = (runtime, value) => runtime === "codex" ? value : optionLabel(String(value).replace(/^claude-/, ""));
const esc = value => KernRichText.escapeHtml(value);
const markdown = value => KernRichText.renderMarkdown(value);
const escAttr = value => esc(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const formatDateTime = value => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  return date.toLocaleString(undefined, { year: "numeric", month: "short", day: "numeric", hour: "numeric", minute: "2-digit", timeZoneName: "short" });
};
const relativeTime = value => {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return String(value || "");
  const minutes = Math.round((Date.now() - date.getTime()) / 60000);
  if (minutes < 1) return "now";
  if (minutes < 60) return `${minutes}m ago`;
  const hours = Math.round(minutes / 60);
  if (hours < 24) return `${hours}h ago`;
  const days = Math.round(hours / 24);
  if (days < 7) return `${days}d ago`;
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
};

window.addEventListener("message", event => {
  const message = event.data;
  if (!message || ![
    "kern-app-api-result",
    "kern-app-copy-text-result",
    "kern-app-upload-file-result",
  ].includes(message.type)) return;
  const callbacks = pending.get(message.request_id);
  if (!callbacks) return;
  pending.delete(message.request_id);
  if (message.ok) callbacks.resolve(message.cancelled ? null : message.body);
  else callbacks.reject(new Error(message.error || "request failed"));
});

function api(method, path, body, timeoutMs = APP_API_TIMEOUT_MS) {
  if (!path.startsWith("/")) throw new Error("app API path must be absolute");
  const requestId = String(nextRequestId++);
  parent.postMessage({ type: "kern-app-api", request_id: requestId, method, path: "/v1/apps/agent_chat/api" + path, body }, "*");
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    setTimeout(() => {
      if (!pending.has(requestId)) return;
      pending.delete(requestId);
      reject(new Error("request timed out"));
    }, timeoutMs);
  });
}

function requestFileUpload(action, selectionId, maximumFiles) {
  const requestId = String(nextRequestId++);
  parent.postMessage({
    type: "kern-app-upload-file",
    request_id: requestId,
    action,
    ...(selectionId ? { selection_id: selectionId } : {}),
    ...(maximumFiles ? { max_files: maximumFiles } : {}),
  }, "*");
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    setTimeout(() => {
      if (!pending.has(requestId)) return;
      pending.delete(requestId);
      reject(new Error("file operation timed out"));
    }, 5 * 60 * 1000);
  });
}

function requestHostCopy(text) {
  const requestId = String(nextRequestId++);
  parent.postMessage({
    type: "kern-app-copy-text",
    request_id: requestId,
    text,
  }, "*");
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    setTimeout(() => {
      if (!pending.has(requestId)) return;
      pending.delete(requestId);
      reject(new Error("copy timed out"));
    }, 30000);
  });
}

function setStatus(message, owner = "action") {
  // An empty message means healthy: hide the banner. A non-empty message is
  // an error to show. Background refreshes may clear their own errors, but
  // never erase a user-action error before the operator can read it.
  if (owner === "refresh" && statusOwner === "action") return;
  statusOwner = message ? owner : null;
  $("status").hidden = !message;
  $("status").textContent = message;
}

async function refresh() {
  try {
    if (!Object.keys(sessionOptions).length) {
      const optionResponse = await api("GET", "/session-options");
      if (!optionResponse.session_options || typeof optionResponse.session_options !== "object") {
        throw new Error("Agent Chat returned invalid session options");
      }
      sessionOptions = optionResponse.session_options;
      setSessionOptions(selectedThreadModel, selectedThreadEffort);
    }
    // A successful /threads already proves the backend is up; when it is down
    // the bridge's own 502 ("app backend unavailable") surfaces as the error.
    const archivedView = showingArchivedThreads;
    const response = await api("GET", archivedView ? "/threads?archived=true" : "/threads");
    if (archivedView !== showingArchivedThreads) return;
    threads = response.threads || [];
    const selectedThread = threads.find(thread => thread.thread_id === selectedThreadId);
    if (selectedThread) {
      const keepPendingSessionChange = sessionConfigurationChanged()
        && selectedThread.status !== "running";
      selectedThreadName = selectedThread.name;
      selectedThreadStatus = selectedThread.status || "idle";
      selectedThreadRuntime = selectedThread.agent_runtime;
      selectedThreadModel = selectedThread.model;
      selectedThreadEffort = selectedThread.effort;
      if (!keepPendingSessionChange) loadSelectedSessionControls();
    }
    renderThreads();
    if (selectedThreadId) await refreshSelectedThread();
    setStatus("", "refresh");
  } catch (error) {
    setStatus(error.message, "refresh");
  }
}

function renderThreads() {
  const key = JSON.stringify([selectedThreadId, showingArchivedThreads, threads]);
  if (key === renderedThreadsKey) return;
  renderedThreadsKey = key;
  if (!threads.length) {
    $("threads").innerHTML = showingArchivedThreads
      ? `<div class="sidebar-empty">No archived threads.</div>`
      : `<div class="sidebar-empty">No threads yet. Send a message below to start one.</div>`;
    return;
  }
  $("threads").innerHTML = threads.map(thread => {
    const active = thread.status === "running";
    return `
    <button class="thread-item${thread.thread_id === selectedThreadId ? " selected" : ""}" data-thread-id="${escAttr(thread.thread_id)}" data-name="${escAttr(thread.name)}" data-runtime="${escAttr(thread.agent_runtime)}" data-model="${escAttr(thread.model)}" data-effort="${escAttr(thread.effort)}" data-status="${escAttr(thread.status || "idle")}" data-archived="${thread.archived ? "true" : "false"}">
      <span class="thread-name"><span>${esc(thread.name)}</span>${active ? `<span class="thread-dot running"></span>` : ""}</span>
      <span class="thread-meta">${esc(runtimeLabel(thread.agent_runtime))} &middot; ${esc(modelLabel(thread.agent_runtime, thread.model))}</span>
      <span class="thread-meta">${esc(relativeTime(thread.last_used_at))}</span>
    </button>`;
  }).join("");
}

function updateComposer() {
  const hasThread = selectedThreadId !== null;
  const readOnly = showingArchivedThreads || selectedThreadArchived;
  const running = hasThread && selectedThreadStatus === "running";
  $("thread-title").textContent = hasThread
    ? selectedThreadName || selectedThreadId
    : showingArchivedThreads
      ? "Archived threads"
      : "New thread";
  const subtitle = hasThread
    ? `${runtimeLabel(selectedThreadRuntime)} · ${modelLabel(selectedThreadRuntime, selectedThreadModel)} · ${optionLabel(selectedThreadEffort)}`
    : "";
  $("thread-subtitle").textContent = subtitle;
  $("thread-subtitle").hidden = !subtitle;
  $("rename-thread").hidden = !hasThread;
  $("archive-thread").hidden = !hasThread;
  $("archive-thread").textContent = selectedThreadArchived ? "Unarchive" : "Archive";
  updateActivityToggle(hasThread);
  $("composer").hidden = readOnly;
  $("composer-hint").hidden = readOnly;
  $("composer-dock").classList.toggle("readonly", readOnly);
  $("new-thread").hidden = showingArchivedThreads;
  $("archived-toggle").textContent = showingArchivedThreads ? "Show active" : "Show archived";
  // Configuration is chosen on the first message and may be changed on the
  // next idle send. The thread id itself is backend-generated, never typed.
  $("new-task-runtime").hidden = false;
  $("new-task-model").hidden = false;
  $("new-task-effort").hidden = false;
  $("composer-running").hidden = !running;
  $("new-task").placeholder = running
    ? selectedThreadRuntime === "hermes"
      ? "Hermes does not support follow-ups while running"
      : "Send another message"
    : hasThread
      ? "Describe what the agent should do next"
      : "Describe a task for the agent";
  updateComposerActions();
}

function updateActivityToggle(hasThread = selectedThreadId !== null) {
  const button = $("activity-toggle");
  button.hidden = !hasThread;
  button.setAttribute("aria-checked", showingActivity ? "true" : "false");
  button.title = showingActivity ? "Hide agent activity" : "Show agent activity";
  $("chat-app").classList.toggle("activity-hidden", !showingActivity);
}

function toggleActivity() {
  showingActivity = !showingActivity;
  updateActivityToggle();
}

function setSessionOptions(preferredModel, preferredEffort) {
  const runtime = $("new-task-runtime").value;
  const models = sessionOptions[runtime] || {};
  const modelValues = Object.keys(models);
  const preservingRecordedSession = (
    selectedThreadId !== null && runtime === selectedThreadRuntime
  );
  if (
    preservingRecordedSession
    && preferredModel
    && !modelValues.includes(preferredModel)
  ) {
    modelValues.push(preferredModel);
  }
  if (!modelValues.length) {
    $("new-task-model").innerHTML = "";
    $("new-task-effort").innerHTML = "";
    $("new-task-model").disabled = true;
    $("new-task-effort").disabled = true;
    updateComposerActions();
    return;
  }
  const model = preferredModel && modelValues.includes(preferredModel)
    ? preferredModel
    : modelValues[0];
  $("new-task-model").innerHTML = modelValues
    .map(value => `<option value="${esc(value)}">${esc(modelLabel(runtime, value))}</option>`)
    .join("");
  $("new-task-model").value = model;
  const efforts = [...(models[model] || [])];
  if (
    preservingRecordedSession
    && model === selectedThreadModel
    && preferredEffort
    && !efforts.includes(preferredEffort)
  ) {
    efforts.push(preferredEffort);
  }
  const effort = preferredEffort && efforts.includes(preferredEffort)
    ? preferredEffort
    : efforts[0];
  $("new-task-effort").innerHTML = efforts
    .map(value => `<option value="${esc(value)}">${esc(optionLabel(value))}</option>`)
    .join("");
  $("new-task-effort").value = effort;
  $("new-task-model").disabled = false;
  $("new-task-effort").disabled = false;
  updateComposerActions();
}

function loadSelectedSessionControls() {
  if (!selectedThreadId || !selectedThreadRuntime) return;
  $("new-task-runtime").value = selectedThreadRuntime;
  setSessionOptions(selectedThreadModel, selectedThreadEffort);
}

function sessionConfigurationChanged() {
  return selectedThreadId !== null && (
    $("new-task-runtime").value !== selectedThreadRuntime
    || $("new-task-model").value !== selectedThreadModel
    || $("new-task-effort").value !== selectedThreadEffort
  );
}

function updateComposerActions() {
  const hasSessionOption = Boolean($("new-task-model").value && $("new-task-effort").value);
  const hasOversizedAttachment = pendingAttachments.some(attachment => attachment.size_bytes > ATTACHMENT_MAX_BYTES);
  const sessionLocked = sendingMessage || (
    selectedThreadId !== null && selectedThreadStatus === "running"
  );
  const runningSessionLocked = selectedThreadId !== null
    && selectedThreadStatus === "running";
  // Hermes has no live input channel, so follow-ups wait until it is idle.
  const activeBlock = selectedThreadId !== null
    && selectedThreadStatus === "running"
    && selectedThreadRuntime === "hermes";
  $("new-task").disabled = activeBlock;
  $("create-task").disabled = (
    activeBlock
    || sendingMessage
    || attachmentActivity !== null
    || hasOversizedAttachment
    || !hasSessionOption
  );
  $("attach-file").disabled = (
    activeBlock
    || sendingMessage
    || attachmentActivity !== null
    || pendingAttachments.length >= ATTACHMENT_LIMIT
  );
  $("new-task-runtime").disabled = sessionLocked;
  $("new-task-model").disabled = sessionLocked || !$("new-task-model").value;
  $("new-task-effort").disabled = sessionLocked || !$("new-task-effort").value;
  $("composer-options").classList.toggle("locked", runningSessionLocked);
  if (!runningSessionLocked) {
    $("composer-options").classList.remove("show-lock-note");
  }
  $("session-change-warning").hidden = (
    showingArchivedThreads
    || selectedThreadArchived
    || selectedThreadStatus === "running"
    || !sessionConfigurationChanged()
  );
}

function renderAttachments() {
  const container = $("attachments");
  container.hidden = attachmentActivity === null && !pendingAttachments.length;
  container.innerHTML = [
    ...pendingAttachments.map(attachment => {
      const tooLarge = attachment.size_bytes > ATTACHMENT_MAX_BYTES;
      return `
        <div class="attachment${tooLarge ? " invalid" : ""}">
          <span class="attachment-name" title="${escAttr(attachment.original_name)}">${esc(attachment.original_name)}</span>
          ${tooLarge ? `<span class="attachment-error">25 MiB max</span>` : ""}
          <button
            class="attachment-clear"
            data-remove-attachment="${escAttr(attachment.selection_id)}"
            aria-label="Remove ${escAttr(attachment.original_name)}"
            title="Remove ${escAttr(attachment.original_name)}"
            ${attachmentActivity !== null || sendingMessage ? "disabled" : ""}
          >&times;</button>
        </div>`;
    }),
    attachmentActivity === null ? "" : `<div class="attachment activity"><span>${esc(attachmentActivity)}</span></div>`,
  ].join("");
  updateComposerActions();
}

async function attachFile() {
  const remaining = ATTACHMENT_LIMIT - pendingAttachments.length;
  if (remaining <= 0) return;
  setStatus("");
  attachmentActivity = "Selecting file…";
  renderAttachments();
  try {
    const response = await requestFileUpload("select", null, remaining);
    if (response === null) return;
    if (!Array.isArray(response.selections) || !response.selections.length) {
      throw new Error("file selection returned an invalid response");
    }
    for (const selection of response.selections) {
      if (
        typeof selection.selection_id !== "string" ||
        typeof selection.original_name !== "string" ||
        typeof selection.size_bytes !== "number"
      ) {
        throw new Error("file selection returned an invalid response");
      }
    }
    if (pendingAttachments.length + response.selections.length > ATTACHMENT_LIMIT) {
      throw new Error(`You can attach up to ${ATTACHMENT_LIMIT} files.`);
    }
    pendingAttachments.push(...response.selections);
  } finally {
    attachmentActivity = null;
    renderAttachments();
  }
}

async function removeAttachment(selectionId) {
  if (sendingMessage) return;
  const index = pendingAttachments.findIndex(attachment => attachment.selection_id === selectionId);
  if (index < 0) return;
  const [attachment] = pendingAttachments.splice(index, 1);
  renderAttachments();
  if (!attachment.file) {
    await requestFileUpload("discard", attachment.selection_id);
  }
}

async function showThread(threadId, name, runtime, model, effort, status, archived) {
  saveSelectedThreadView();
  selectedThreadId = threadId;
  selectedThreadName = name;
  selectedThreadRuntime = runtime;
  selectedThreadModel = model;
  selectedThreadEffort = effort;
  selectedThreadStatus = status || "idle";
  selectedThreadArchived = archived;
  loadSelectedSessionControls();
  restoreThreadView(threadId);
  updateComposer();
  renderThreads();
  renderThreadHistory();
  await refreshSelectedThread();
}

async function refreshSelectedThread() {
  if (!selectedThreadId) {
    renderThreadHistory();
    return;
  }
  // Capture the id: a thread switch mid-flight must not let a stale response
  // land in the newly selected thread's state.
  const threadId = selectedThreadId;
  await refreshThreadEvents(threadId);
  if (threadId !== selectedThreadId) return;
  updateComposer();
  renderThreadHistory();
}

function saveSelectedThreadView() {
  if (!selectedThreadId) return;
  const scroller = $("chat-scroll");
  // Refresh insertion order so the oldest unvisited thread is evicted first.
  threadViewStates.delete(selectedThreadId);
  threadViewStates.set(selectedThreadId, {
    events: threadEvents,
    oldestSeq: threadEventsOldestSeq,
    newestSeq: threadEventsNewestSeq,
    initialized: threadEventsInitialized,
    hasOlder: hasOlderThreadEvents,
    scrollTop: scroller ? scroller.scrollTop : lastChatScrollTop,
  });
  if (threadViewStates.size > VIEW_STATE_LIMIT) {
    threadViewStates.delete(threadViewStates.keys().next().value);
  }
}

function restoreThreadView(threadId) {
  const state = threadViewStates.get(threadId);
  if (!state) {
    resetThreadEvents();
    return;
  }
  threadEvents = state.events;
  threadEventsOldestSeq = state.oldestSeq;
  threadEventsNewestSeq = state.newestSeq;
  threadEventsInitialized = state.initialized;
  hasOlderThreadEvents = state.hasOlder;
  loadingOlderThreadEvents = false;
  lastChatScrollTop = state.scrollTop;
  restoredChatScrollTop = state.scrollTop;
  renderHistoryLoader();
}

function resetThreadEvents() {
  threadEvents = [];
  threadEventsOldestSeq = null;
  threadEventsNewestSeq = 0;
  threadEventsInitialized = false;
  hasOlderThreadEvents = false;
  loadingOlderThreadEvents = false;
  lastChatScrollTop = 0;
  restoredChatScrollTop = null;
  renderHistoryLoader();
}

function mergeThreadEvents(events) {
  const bySeq = new Map(threadEvents.map(event => [event.seq, event]));
  for (const event of events) bySeq.set(event.seq, event);
  const ordered = Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
  // Keep one bounded snapshot per semantic activity. Streaming command
  // deltas can otherwise retain and repeatedly rebuild an unbounded list of
  // large chunks for the lifetime of the browser tab.
  threadEvents = KernRichText.compactActivityEvents(ordered);
}

async function refreshThreadEvents(threadId) {
  if (!threadEventsInitialized) {
    // No cursor means "latest page". Prefetch three bounded pages so a first
    // view normally has enough history to scroll without making the operator
    // click the top loader, while every individual response stays below the
    // app bridge's fixed size ceiling.
    const response = await api(
      "GET",
      `/threads/${encodeURIComponent(threadId)}/events`,
    );
    if (threadId !== selectedThreadId) return;
    const events = response.events || [];
    mergeThreadEvents(events);
    if (events.length) {
      threadEventsOldestSeq = events[0].seq;
      threadEventsNewestSeq = events[events.length - 1].seq;
    }
    let oldestPage = events;
    for (
      let page = 1;
      page < INITIAL_EVENT_PAGES
      && oldestPage.length === EVENTS_PAGE
      && threadEventsOldestSeq !== null;
      page += 1
    ) {
      const before = threadEventsOldestSeq;
      const olderResponse = await api(
        "GET",
        `/threads/${encodeURIComponent(threadId)}/events?before=${before}`,
      );
      if (threadId !== selectedThreadId) return;
      oldestPage = (olderResponse.events || []).filter(event => event.seq < before);
      if (oldestPage.length) {
        mergeThreadEvents(oldestPage);
        threadEventsOldestSeq = oldestPage[0].seq;
      }
    }
    hasOlderThreadEvents = oldestPage.length === EVENTS_PAGE;
    threadEventsInitialized = true;
    renderHistoryLoader();
    return;
  }
  // Once the tail is loaded, forward paging drains only events that arrived
  // after it. This keeps live activity complete without revisiting history.
  for (;;) {
    const response = await api(
      "GET",
      `/threads/${encodeURIComponent(threadId)}/events?since=${threadEventsNewestSeq}`,
    );
    if (threadId !== selectedThreadId) return;
    const events = response.events || [];
    // Only accept events past the cursor, so a server that re-sends an
    // overlapping page can never double-append into the stream.
    const fresh = events.filter(event => event.seq > threadEventsNewestSeq);
    if (fresh.length) {
      mergeThreadEvents(fresh);
      threadEventsNewestSeq = fresh[fresh.length - 1].seq;
      if (threadEventsOldestSeq === null) threadEventsOldestSeq = fresh[0].seq;
    }
    // Keep paging only while the cursor advanced by a full page. A short or
    // no-progress page means the live tail is caught up.
    if (fresh.length < EVENTS_PAGE) return;
  }
}

async function loadOlderThreadEvents() {
  if (
    !selectedThreadId
    || !threadEventsInitialized
    || !hasOlderThreadEvents
    || loadingOlderThreadEvents
    || threadEventsOldestSeq === null
  ) return;
  const threadId = selectedThreadId;
  const before = threadEventsOldestSeq;
  loadingOlderThreadEvents = true;
  renderHistoryLoader();
  try {
    const response = await api(
      "GET",
      `/threads/${encodeURIComponent(threadId)}/events?before=${before}`,
    );
    if (threadId !== selectedThreadId) return;
    const events = response.events || [];
    const older = events.filter(event => event.seq < before);
    const scroller = $("chat-scroll");
    // Capture immediately before mutating the DOM so reading movement during
    // the request is respected. Compensating by the added scroll height keeps
    // the same content under the operator's eyes as older turns are prepended.
    const previousHeight = scroller.scrollHeight;
    const previousTop = scroller.scrollTop;
    if (older.length) {
      mergeThreadEvents(older);
      threadEventsOldestSeq = older[0].seq;
    }
    hasOlderThreadEvents = older.length === EVENTS_PAGE;
    renderThreadHistory();
    scroller.scrollTop = previousTop + (scroller.scrollHeight - previousHeight);
  } finally {
    if (threadId === selectedThreadId) {
      loadingOlderThreadEvents = false;
      renderHistoryLoader();
    }
  }
}

function renderHistoryLoader() {
  const loader = $("history-loader");
  if (!loader) return;
  loader.hidden = !selectedThreadId || !hasOlderThreadEvents;
  loader.dataset.oldestSeq = threadEventsOldestSeq === null ? "" : String(threadEventsOldestSeq);
  const button = $("load-earlier");
  button.disabled = loadingOlderThreadEvents;
  button.textContent = loadingOlderThreadEvents ? "Loading earlier messages…" : "Load earlier messages";
}

function renderThreadHistory() {
  const key = JSON.stringify([
    selectedThreadId,
    showingArchivedThreads,
    selectedThreadStatus,
    threadEventsOldestSeq,
    threadEventsNewestSeq,
    threadEvents.length,
  ]);
  if (key === renderedHistoryKey) return;
  renderedHistoryKey = key;
  const switched = renderedHistoryThread !== selectedThreadId;
  renderedHistoryThread = selectedThreadId;
  const detail = $("thread-detail");
  if (!selectedThreadId) {
    renderedEntryHtml.clear();
    renderHistoryLoader();
    detail.innerHTML = showingArchivedThreads
      ? `<div class="chat-hero">
          <h2>Archived threads</h2>
          <p>Select a thread to read it, or return to active threads.</p>
        </div>`
      : `<div class="chat-hero">
          <h2>What should the agent work on?</h2>
          <p>Messages continue in the same agent session. Supported agents can also receive another message while working.</p>
        </div>`;
    return;
  }
  renderHistoryLoader();
  const scroller = $("chat-scroll");
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
  // Patch entries in place instead of rebuilding the whole history, so an
  // in-flight touch scroll (and its momentum) survives polling.
  const ordered = threadEvents.filter(event => (
    ["thread.message", "thread.activity", "thread.error", "thread.stopped"]
      .includes(event.event_type)
  ));
  if (switched || !ordered.length) {
    renderedEntryHtml.clear();
    detail.innerHTML = ordered.length
      ? ""
      : `<div class="chat-hero"><p>No retained messages for this thread yet.</p></div>`;
  }
  const openActivities = new Set(
    Array.from(document.querySelectorAll(".activity-card[open]"))
      .map(card => card.dataset.activityId),
  );
  if (ordered.length) {
    ordered.forEach((event, index) => {
      const entryKey = `event-${event.seq}`;
      const html = renderThreadEntry(event, openActivities);
      const current = detail.children[index];
      if (current && current.dataset.entryId === entryKey) {
        if (renderedEntryHtml.get(entryKey) !== html) {
          detail.replaceChild(threadEntryElement(html), current);
        }
      } else {
        detail.insertBefore(threadEntryElement(html), current || null);
      }
      renderedEntryHtml.set(entryKey, html);
    });
    while (detail.children.length > ordered.length) {
      renderedEntryHtml.delete(detail.lastElementChild.dataset.entryId);
      detail.lastElementChild.remove();
    }
  }
  // Instant jump when landing in a thread; smooth glide when the operator
  // just sent a message; stick to the bottom while reading there.
  if (switched && restoredChatScrollTop !== null) {
    scroller.scrollTop = restoredChatScrollTop;
  } else if (switched) scroller.scrollTop = scroller.scrollHeight;
  else if (forceScrollBottom) scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  else if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  restoredChatScrollTop = null;
  forceScrollBottom = false;
}

function threadEntryElement(html) {
  const template = document.createElement("template");
  template.innerHTML = html.trim();
  return template.content.firstElementChild;
}

function renderActivity(value, openActivities) {
  const activityId = String(value.activity_id || "");
  const phase = value.phase === "completed" ? "completed" : "started";
  const requestedKind = String(value.kind || "status").replace(/[^a-z_]/g, "");
  const kind = Object.prototype.hasOwnProperty.call(ACTIVITY_PRESENTATION, requestedKind)
    ? requestedKind
    : "status";
  const presentation = ACTIVITY_PRESENTATION[kind];
  const rawStatus = typeof value.status === "string" ? value.status : "";
  const showStatus = rawStatus && !["completed", "running"].includes(rawStatus.toLowerCase());
  const statusTone = /(?:fail|error|denied|exit\s+[1-9])/i.test(rawStatus) ? " failed" : "";
  const status = showStatus
    ? `<span class="activity-status${statusTone}">${esc(rawStatus)}</span>`
    : "";
  const body = [
    value.detail
      ? `<section><div class="activity-label">${presentation.detail}</div><pre>${esc(value.detail)}</pre></section>`
      : "",
    value.output
      ? `<section><div class="activity-label">${presentation.output}</div><pre>${esc(value.output)}</pre></section>`
      : "",
  ].join("");
  const summary = `
      <span class="activity-icon" aria-hidden="true">${presentation.icon}</span>
      <span class="activity-heading">
        <span class="activity-title">${esc(value.title || "Agent activity")}</span>
        <span class="activity-kind">${presentation.label}</span>
      </span>
      ${status}
      ${phase === "started" ? `<span class="activity-phase">Started</span>` : ""}`;
  const cardClass = `activity-card activity-${esc(kind)} ${phase}`;
  const label = `${presentation.label}: ${String(value.title || "Agent activity")}`;
  if (!body) {
    return `
      <div class="${cardClass} activity-static" data-activity-id="${escAttr(activityId)}" role="status" aria-label="${escAttr(label)}">
        <div class="activity-summary">${summary}</div>
      </div>`;
  }
  const open = openActivities.has(activityId);
  return `
    <details class="${cardClass}" data-activity-id="${escAttr(activityId)}" aria-label="${escAttr(label)}"${open ? " open" : ""}>
      <summary>${summary}</summary>
      <div class="activity-body">${body}</div>
    </details>`;
}

function renderThreadEntry(event, openActivities) {
  const entryId = `event-${event.seq}`;
  const payload = event.payload || {};
  if (event.event_type === "thread.activity") {
    return `<article class="thread-entry" data-entry-id="${entryId}">
      ${renderActivity(payload.activity || {}, openActivities)}
    </article>`;
  }
  if (event.event_type === "thread.error") {
    return `<article class="thread-entry thread-error" data-entry-id="${entryId}">
      ${esc(payload.error_message || "The agent stopped because of an error.")}
    </article>`;
  }
  if (event.event_type === "thread.stopped") {
    return `<article class="thread-entry thread-stopped" data-entry-id="${entryId}">
      Agent stopped
    </article>`;
  }
  const text = typeof payload.message === "string" ? payload.message : "";
  if (payload.source === "user") {
    return `<article class="thread-entry thread-user" data-entry-id="${entryId}">
      <div class="bubble"><pre>${esc(text)}</pre></div>
    </article>`;
  }
  return `<article class="thread-entry thread-agent md-content" data-entry-id="${entryId}">
    ${markdown(text)}
  </article>`;
}

async function sendMessage() {
  if (sendingMessage || $("create-task").disabled) return;
  sendingMessage = true;
  updateComposerActions();
  try {
    await sendMessageUnlocked();
  } finally {
    sendingMessage = false;
    updateComposerActions();
  }
}

async function sendMessageUnlocked() {
  if (showingArchivedThreads || selectedThreadArchived) return;
  const message = $("new-task").value.trim();
  const runtime = $("new-task-runtime").value;
  const model = $("new-task-model").value;
  const effort = $("new-task-effort").value;
  if ((!message && !pendingAttachments.length) || !model || !effort) return;
  setStatus("");
  // A request without thread_id asks the backend to open a new thread with a
  // generated successive name (thread-1, thread-2, ...).
  const startingNewThread = selectedThreadId === null;
  const changingSession = sessionConfigurationChanged();
  const request = { input_message: "" };
  if (startingNewThread || changingSession) {
    Object.assign(request, { agent_runtime: runtime, model, effort });
  }
  if (!startingNewThread) request.thread_id = selectedThreadId;
  for (const [index, attachment] of pendingAttachments.entries()) {
    if (attachment.file) continue;
    attachmentActivity = `Uploading ${index + 1} of ${pendingAttachments.length}…`;
    renderAttachments();
    try {
      const response = await requestFileUpload("upload", attachment.selection_id);
      if (!response.file || typeof response.file.path !== "string" || typeof response.file.name !== "string") {
        throw new Error("file upload returned an invalid response");
      }
      attachment.file = response.file;
    } finally {
      attachmentActivity = null;
      renderAttachments();
    }
  }
  const uploadedFiles = pendingAttachments.map(attachment => attachment.file);
  const fileReferences = uploadedFiles
    .map(file => `[User-uploaded file: ${file.path}]`)
    .join("\n");
  const inputMessage = uploadedFiles.length
    ? `${message || (uploadedFiles.length === 1 ? "Please review the uploaded file." : "Please review the uploaded files.")}\n\n${fileReferences}`
    : message;
  request.input_message = inputMessage;
  attachmentActivity = "Sending…";
  renderAttachments();
  let result;
  try {
    result = await api("POST", "/messages", request, AGENT_DELIVERY_TIMEOUT_MS);
  } finally {
    attachmentActivity = null;
    renderAttachments();
  }
  $("new-task").value = "";
  pendingAttachments = [];
  renderAttachments();
  autosizeComposer();
  if (startingNewThread) {
    // A brand-new thread has no prior event stream to keep; start its
    // newest-page accumulator clean so its first poll reads only this work.
    resetThreadEvents();
  }
  selectedThreadId = result.thread_id;
  if (startingNewThread) selectedThreadName = result.thread_id;
  selectedThreadRuntime = runtime;
  selectedThreadModel = model;
  selectedThreadEffort = effort;
  selectedThreadStatus = "running";
  forceScrollBottom = true;
  updateComposer();
  await refresh();
}

async function stopRunningTurn() {
  if (selectedThreadStatus !== "running" || !selectedThreadId) return;
  if (!confirm("Stop the agent?")) return;
  attachmentActivity = "Stopping…";
  renderAttachments();
  try {
    await api(
      "POST",
      `/threads/${encodeURIComponent(selectedThreadId)}/stop`,
      undefined,
      AGENT_DELIVERY_TIMEOUT_MS,
    );
  } finally {
    attachmentActivity = null;
    renderAttachments();
  }
  await refresh();
}

async function setSelectedThreadArchived() {
  if (!selectedThreadId) return;
  const action = selectedThreadArchived ? "unarchive" : "archive";
  await api("POST", `/threads/${encodeURIComponent(selectedThreadId)}/${action}`);
  clearSelectedThread();
  await refresh();
}

async function renameSelectedThread() {
  if (!selectedThreadId) return;
  const threadId = selectedThreadId;
  const requestedName = prompt("Rename thread (max 100 characters):", selectedThreadName || threadId);
  if (requestedName === null) return;
  const name = requestedName.trim();
  if (!name) {
    setStatus("Thread name cannot be empty.");
    return;
  }
  const response = await api(
    "PUT",
    `/threads/${encodeURIComponent(threadId)}/name`,
    { name },
  );
  const renamedName = response.thread.name;
  threads = threads.map(thread => (
    thread.thread_id === threadId
      ? { ...thread, name: renamedName }
      : thread
  ));
  if (selectedThreadId === threadId) {
    selectedThreadName = renamedName;
    updateComposer();
  }
  renderThreads();
  setStatus("");
}

function clearSelectedThread() {
  saveSelectedThreadView();
  selectedThreadId = null;
  selectedThreadName = null;
  selectedThreadRuntime = null;
  selectedThreadModel = null;
  selectedThreadEffort = null;
  selectedThreadStatus = "idle";
  selectedThreadArchived = false;
  resetThreadEvents();
  updateComposer();
  renderThreadHistory();
  renderThreads();
}

function startNewThread() {
  showingArchivedThreads = false;
  clearSelectedThread();
}

async function toggleArchivedThreads() {
  showingArchivedThreads = !showingArchivedThreads;
  clearSelectedThread();
  await refresh();
}

// Must match the drawer breakpoint in agent_chat.css.
const drawerMedia = window.matchMedia("(max-width: 720px)");

function setSidebarOpen(open, restoreFocus = false) {
  const mobile = drawerMedia.matches;
  const isOpen = mobile && open;
  const pane = document.querySelector(".thread-pane");
  $("chat-app").classList.toggle("sidebar-open", isOpen);
  // The closed drawer is only moved off-canvas by a transform, so drop it
  // (and, while open, the pane behind it) from the tab order the same way
  // the host mobile nav does.
  pane.inert = mobile && !isOpen;
  document.querySelector(".chat-main").inert = isOpen;
  $("sidebar-backdrop").hidden = !isOpen;
  $("sidebar-open").setAttribute("aria-expanded", String(isOpen));
  if (isOpen) $("sidebar-close").focus();
  else if (restoreFocus && mobile) $("sidebar-open").focus();
}

function autosizeComposer() {
  const area = $("new-task");
  area.style.height = "auto";
  area.style.height = `${Math.min(area.scrollHeight, 200)}px`;
}

document.addEventListener("click", event => {
  const linkButton = event.target.closest && event.target.closest(".md-copy-link");
  if (linkButton) {
    const original = linkButton.textContent;
    requestHostCopy(linkButton.dataset.copyHref || "").then(() => {
      linkButton.textContent = "Copied link";
      setTimeout(() => { linkButton.textContent = original; }, 1200);
    }).catch(error => setStatus(error.message));
    return;
  }
  const copyButton = event.target.closest && event.target.closest(".md-copy");
  if (copyButton) {
    const code = copyButton.closest(".md-code").querySelector("code").textContent;
    requestHostCopy(code).then(() => {
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy"; }, 1200);
    }).catch(error => setStatus(error.message));
    return;
  }
  const thread = event.target.closest && event.target.closest(".thread-item");
  if (thread) {
    setSidebarOpen(false);
    showThread(
      thread.dataset.threadId,
      thread.dataset.name,
      thread.dataset.runtime,
      thread.dataset.model,
      thread.dataset.effort,
      thread.dataset.status,
      thread.dataset.archived === "true",
    ).catch(error => setStatus(error.message));
    return;
  }
  const removeAttachmentButton = event.target.closest && event.target.closest("button[data-remove-attachment]");
  if (removeAttachmentButton) {
    removeAttachment(removeAttachmentButton.dataset.removeAttachment).catch(error => setStatus(error.message));
  }
});

$("new-thread").addEventListener("click", () => {
  setSidebarOpen(false);
  $("new-task-runtime").value = "codex";
  setSessionOptions();
  startNewThread();
  $("new-task").focus();
});
$("archived-toggle").addEventListener("click", () => toggleArchivedThreads().catch(error => setStatus(error.message)));
$("rename-thread").addEventListener("click", () => renameSelectedThread().catch(error => setStatus(error.message)));
$("archive-thread").addEventListener("click", () => setSelectedThreadArchived().catch(error => setStatus(error.message)));
$("activity-toggle").addEventListener("click", toggleActivity);
$("load-earlier").addEventListener("click", () => loadOlderThreadEvents().catch(error => setStatus(error.message)));
$("chat-scroll").addEventListener("scroll", () => {
  const scroller = $("chat-scroll");
  const movedUp = scroller.scrollTop < lastChatScrollTop;
  lastChatScrollTop = scroller.scrollTop;
  if (!movedUp || scroller.scrollTop > 160) return;
  loadOlderThreadEvents().catch(error => setStatus(error.message));
}, { passive: true });
$("stop-task").addEventListener("click", () => stopRunningTurn().catch(error => setStatus(error.message)));
$("create-task").addEventListener("click", () => sendMessage().catch(error => setStatus(error.message)));
$("attach-file").addEventListener("click", () => attachFile().catch(error => setStatus(error.message)));
$("new-task-runtime").addEventListener("change", () => setSessionOptions());
$("new-task-model").addEventListener("change", () => {
  const model = $("new-task-model").value;
  setSessionOptions(model, model === selectedThreadModel ? selectedThreadEffort : undefined);
});
$("new-task-effort").addEventListener("change", updateComposerActions);
$("composer-options").addEventListener("mouseenter", () => {
  if ($("composer-options").classList.contains("locked")) {
    $("composer-options").classList.add("show-lock-note");
  }
});
$("composer-options").addEventListener("mouseleave", () => {
  $("composer-options").classList.remove("show-lock-note");
});
$("new-task").addEventListener("input", autosizeComposer);
$("new-task").addEventListener("keydown", event => {
  const sendKey = event.key === "Enter" && !event.isComposing && (!event.shiftKey || event.metaKey || event.ctrlKey);
  if (!sendKey) return;
  event.preventDefault();
  sendMessage().catch(error => setStatus(error.message));
});
$("sidebar-open").addEventListener("click", () => setSidebarOpen(true));
$("sidebar-close").addEventListener("click", () => setSidebarOpen(false, true));
$("sidebar-backdrop").addEventListener("click", () => setSidebarOpen(false, true));
drawerMedia.addEventListener("change", () => setSidebarOpen(false));

setSessionOptions();
updateComposer();
renderThreadHistory();
autosizeComposer();
renderAttachments();
setSidebarOpen(false);
async function scheduleRefresh() {
  await refresh();
  const active = threads.some(thread => thread.status === "running");
  setTimeout(scheduleRefresh, active ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS);
}
scheduleRefresh();
