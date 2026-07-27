const pending = new Map();
let nextRequestId = 1;
let threads = [];
let tasks = [];
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
const EVENTS_PAGE = 6;
const ACTIVE_REFRESH_MS = 1000;
const IDLE_REFRESH_MS = 5000;
let selectedThreadId = null;
let selectedThreadName = null;
let selectedThreadRuntime = null;
let selectedThreadModel = null;
let selectedThreadEffort = null;
let selectedThreadArchived = false;
let showingArchivedThreads = false;
let sessionOptions = {};
let pendingAttachments = [];
let attachmentActivity = null;
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
// changed, so a steering draft or the reading scroll position survives
// refreshes that bring nothing new.
let renderedThreadsKey = null;
let renderedHistoryKey = null;
let renderedHistoryThread = null;
// Per-task rendered HTML, so a poll only patches turns that actually changed.
const renderedTurnHtml = new Map();
let forceScrollBottom = false;

const $ = id => document.getElementById(id);
const runtimeLabel = runtime => runtime === "claude_code" ? "Claude Code" : runtime === "codex" ? "Codex" : runtime === "hermes" ? "Hermes" : runtime;
const optionLabel = value => value.split(/[-_]/).map(part => part.charAt(0).toUpperCase() + part.slice(1)).join(" ");
// Claude Code model ids carry the provider prefix ("claude-opus-5"); the
// runtime name already says Claude Code, so the pill reads "Opus 5".
const modelLabel = (runtime, value) => runtime === "codex" ? value : optionLabel(String(value).replace(/^claude-/, ""));
const esc = value => KernRichText.escapeHtml(value);
const markdown = value => KernRichText.renderMarkdown(value);
const escAttr = value => esc(value).replaceAll('"', "&quot;").replaceAll("'", "&#39;");
const badge = value => `<span class="status ${esc(value)}">${esc(value)}</span>`;
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

function api(method, path, body) {
  if (!path.startsWith("/")) throw new Error("app API path must be absolute");
  const requestId = String(nextRequestId++);
  parent.postMessage({ type: "kern-app-api", request_id: requestId, method, path: "/v1/apps/agent_chat/api" + path, body }, "*");
  return new Promise((resolve, reject) => {
    pending.set(requestId, { resolve, reject });
    setTimeout(() => {
      if (!pending.has(requestId)) return;
      pending.delete(requestId);
      reject(new Error("request timed out"));
    }, 30000);
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

function setStatus(message) {
  // An empty message means healthy: hide the banner. A non-empty message is
  // an error to show.
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
    if (selectedThread) selectedThreadName = selectedThread.name;
    renderThreads();
    if (selectedThreadId) await refreshSelectedThread();
    setStatus("");
  } catch (error) {
    setStatus(error.message);
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
    const active = (thread.active_tasks || []).length > 0;
    const count = `${thread.task_count} task${thread.task_count === 1 ? "" : "s"}`;
    return `
    <button class="thread-item${thread.thread_id === selectedThreadId ? " selected" : ""}" data-thread-id="${escAttr(thread.thread_id)}" data-name="${escAttr(thread.name)}" data-runtime="${escAttr(thread.agent_runtime)}" data-model="${escAttr(thread.model)}" data-effort="${escAttr(thread.effort)}" data-archived="${thread.archived ? "true" : "false"}">
      <span class="thread-name"><span>${esc(thread.name)}</span>${active ? `<span class="thread-dot running"></span>` : ""}</span>
      <span class="thread-meta">${esc(runtimeLabel(thread.agent_runtime))} &middot; ${esc(modelLabel(thread.agent_runtime, thread.model))}</span>
      <span class="thread-meta">${esc(count)} &middot; ${esc(relativeTime(thread.last_used_at))}</span>
    </button>`;
  }).join("");
}

function updateComposer() {
  const hasThread = selectedThreadId !== null;
  const readOnly = showingArchivedThreads || selectedThreadArchived;
  const queuedTask = tasks.find(task => task.status === "queued");
  const runningTask = tasks.find(task => task.status === "running");
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
  $("composer").hidden = readOnly;
  $("composer-hint").hidden = readOnly;
  $("composer-dock").classList.toggle("readonly", readOnly);
  $("new-thread").hidden = showingArchivedThreads;
  $("archived-toggle").textContent = showingArchivedThreads ? "Show active" : "Show archived";
  // Follow-up tasks reuse the thread's stored session configuration, so the
  // pills only show while composing the first task of a new thread. The
  // thread id itself is backend-generated, never typed.
  $("new-task-runtime").hidden = hasThread;
  $("new-task-model").hidden = hasThread;
  $("new-task-effort").hidden = hasThread;
  $("composer-running").hidden = !runningTask;
  $("new-task").placeholder = queuedTask
    ? "Waiting for the agent to start…"
    : runningTask
      ? runningTask.agent_runtime === "hermes"
        ? "Hermes does not support follow-ups while running"
        : "Send a follow-up to steer the running task"
      : hasThread
        ? "Describe what the agent should do next"
        : "Describe a task for the agent";
  if (hasThread) {
    $("new-task-runtime").value = selectedThreadRuntime;
    setSessionOptions(selectedThreadModel, selectedThreadEffort);
  }
  updateComposerActions();
}

function setSessionOptions(preferredModel, preferredEffort) {
  const runtime = $("new-task-runtime").value;
  const models = sessionOptions[runtime] || {};
  const modelValues = Object.keys(models);
  if (!modelValues.length) {
    $("new-task-model").innerHTML = "";
    $("new-task-effort").innerHTML = "";
    $("new-task-model").disabled = true;
    $("new-task-effort").disabled = true;
    updateComposerActions();
    return;
  }
  const model = preferredModel && models[preferredModel] ? preferredModel : modelValues[0];
  $("new-task-model").innerHTML = modelValues
    .map(value => `<option value="${esc(value)}">${esc(modelLabel(runtime, value))}</option>`)
    .join("");
  $("new-task-model").value = model;
  const efforts = models[model];
  const effort = preferredEffort && efforts.includes(preferredEffort) ? preferredEffort : efforts[0];
  $("new-task-effort").innerHTML = efforts
    .map(value => `<option value="${esc(value)}">${esc(optionLabel(value))}</option>`)
    .join("");
  $("new-task-effort").value = effort;
  $("new-task-model").disabled = false;
  $("new-task-effort").disabled = false;
  updateComposerActions();
}

function updateComposerActions() {
  const hasSessionOption = Boolean($("new-task-model").value && $("new-task-effort").value);
  const hasOversizedAttachment = pendingAttachments.some(attachment => attachment.size_bytes > ATTACHMENT_MAX_BYTES);
  const queuedTask = tasks.some(task => task.status === "queued");
  const unsteerableRunningTask = tasks.some(
    task => task.status === "running" && task.agent_runtime === "hermes",
  );
  const activeBlock = queuedTask || unsteerableRunningTask;
  $("new-task").disabled = activeBlock;
  $("create-task").disabled = (
    activeBlock
    || attachmentActivity !== null
    || hasOversizedAttachment
    || !hasSessionOption
  );
  $("attach-file").disabled = (
    activeBlock
    || attachmentActivity !== null
    || pendingAttachments.length >= ATTACHMENT_LIMIT
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
            ${attachmentActivity !== null ? "disabled" : ""}
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
  const index = pendingAttachments.findIndex(attachment => attachment.selection_id === selectionId);
  if (index < 0) return;
  const [attachment] = pendingAttachments.splice(index, 1);
  renderAttachments();
  if (!attachment.file) {
    await requestFileUpload("discard", attachment.selection_id);
  }
}

async function showThread(threadId, name, runtime, model, effort, archived) {
  selectedThreadId = threadId;
  selectedThreadName = name;
  selectedThreadRuntime = runtime;
  selectedThreadModel = model;
  selectedThreadEffort = effort;
  selectedThreadArchived = archived;
  tasks = [];
  resetThreadEvents();
  updateComposer();
  renderThreads();
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
  const response = await api("GET", `/threads/${encodeURIComponent(threadId)}/tasks`);
  if (threadId !== selectedThreadId) return;
  tasks = response.tasks || [];
  await refreshThreadEvents(threadId);
  if (threadId !== selectedThreadId) return;
  updateComposer();
  renderThreadHistory();
}

function resetThreadEvents() {
  threadEvents = [];
  threadEventsOldestSeq = null;
  threadEventsNewestSeq = 0;
  threadEventsInitialized = false;
  hasOlderThreadEvents = false;
  loadingOlderThreadEvents = false;
  lastChatScrollTop = 0;
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
    // No cursor means "latest page"; opening a long thread therefore paints
    // its useful tail after one bounded request instead of draining history.
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
    hasOlderThreadEvents = events.length === EVENTS_PAGE;
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
    loadingOlderThreadEvents = false;
    if (threadId === selectedThreadId) renderHistoryLoader();
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
    tasks,
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
    renderedTurnHtml.clear();
    renderHistoryLoader();
    detail.innerHTML = showingArchivedThreads
      ? `<div class="chat-hero">
          <h2>Archived threads</h2>
          <p>Select a thread to read it, or return to active threads.</p>
        </div>`
      : `<div class="chat-hero">
          <h2>What should the agent work on?</h2>
          <p>Send one message at a time. A follow-up steers running work; otherwise it starts the next turn in the same agent session.</p>
        </div>`;
    return;
  }
  renderHistoryLoader();
  const scroller = $("chat-scroll");
  const nearBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60;
  // Patch turns in place instead of rebuilding the whole history: a poll that
  // brings a task-field change only touches that task's article, so an
  // in-flight touch scroll (and its momentum) survives the refresh.
  const eventsByTask = new Map();
  for (const event of threadEvents) {
    if (!["task.message", "task.activity"].includes(event.event_type)) continue;
    if (!eventsByTask.has(event.task_id)) eventsByTask.set(event.task_id, []);
    eventsByTask.get(event.task_id).push(event);
  }
  const ordered = tasks
    .filter(task => task.status === "queued" || eventsByTask.has(task.task_id))
    .sort((a, b) => String(a.created_at).localeCompare(String(b.created_at)));
  if (switched || !ordered.length) {
    renderedTurnHtml.clear();
    detail.innerHTML = ordered.length
      ? ""
      : `<div class="chat-hero"><p>No retained messages for this thread yet.</p></div>`;
  }
  const openActivities = new Set(
    Array.from(document.querySelectorAll(".activity-card[open]"))
      .map(card => card.dataset.activityId),
  );
  if (ordered.length) {
    ordered.forEach((task, index) => {
      const html = renderTurn(task, eventsByTask.get(task.task_id) || [], openActivities);
      const current = detail.children[index];
      if (current && current.dataset.taskId === task.task_id) {
        if (renderedTurnHtml.get(task.task_id) !== html) detail.replaceChild(turnElement(html), current);
      } else {
        detail.insertBefore(turnElement(html), current || null);
      }
      renderedTurnHtml.set(task.task_id, html);
    });
    while (detail.children.length > ordered.length) {
      renderedTurnHtml.delete(detail.lastElementChild.dataset.taskId);
      detail.lastElementChild.remove();
    }
  }
  // Instant jump when landing in a thread; smooth glide when the operator
  // just sent a message; stick to the bottom while reading there.
  if (switched) scroller.scrollTop = scroller.scrollHeight;
  else if (forceScrollBottom) scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  else if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  forceScrollBottom = false;
}

function turnElement(html) {
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
      ${phase === "started" ? `<span class="activity-phase">Running</span>` : ""}`;
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

function renderTurn(task, events, openActivities) {
  // Queued work has no message event yet, so its compact task snapshot is the
  // temporary opening bubble. Once claimed, the snapshot is ignored and the
  // complete conversation comes only from task.message/task.activity events.
  const mergedActivities = new Map();
  for (const event of events) {
    const value = event.payload && event.payload.activity;
    if (!value || typeof value !== "object") continue;
    const activityId = String(value.activity_id || `event-${event.seq}`);
    const previous = mergedActivities.get(activityId) || {};
    const genericUpdate = ["Tool result", "Tool progress", "Command output"].includes(value.title);
    const appendedOutput = value.append_output && previous.output
      ? `${previous.output}${value.output || ""}`
      : value.output;
    mergedActivities.set(activityId, {
      ...previous,
      ...value,
      // Claude tool-result blocks identify the original call but do not
      // repeat its friendly name/kind; command output deltas are similar.
      title: genericUpdate && previous.title ? previous.title : value.title,
      kind: genericUpdate && previous.kind ? previous.kind : value.kind,
      output: appendedOutput,
      activity_id: activityId,
    });
  }
  const stream = [];
  let userMessageCount = 0;
  const renderedActivities = new Set();
  for (const event of task.status === "queued" ? [] : events) {
    if (event.event_type === "task.activity") {
      const value = event.payload && event.payload.activity;
      if (!value || typeof value !== "object") continue;
      const activityId = String(value.activity_id || `event-${event.seq}`);
      if (renderedActivities.has(activityId)) continue;
      renderedActivities.add(activityId);
      stream.push(renderActivity(mergedActivities.get(activityId), openActivities));
      continue;
    }
    const text = event.payload && event.payload.message;
    if (typeof text !== "string" || !text) continue;
    if (event.payload.source === "user") {
      const steerClass = userMessageCount > 0 ? " steer-bubble" : "";
      stream.push(`<div class="turn-user"><div class="bubble${steerClass}"><pre>${esc(text)}</pre></div></div>`);
      userMessageCount += 1;
    } else {
      stream.push(`<div class="turn-agent md-content">${markdown(text)}</div>`);
    }
  }
  const queuedPrompt = task.status === "queued"
    ? `<div class="turn-user"><div class="bubble"><pre>${esc(task.input_message)}</pre></div></div>`
    : "";
  return `
    <article class="turn" data-task-id="${esc(task.task_id)}">
      ${queuedPrompt}
      <div class="turn-meta">
        ${task.status === "completed" ? "" : badge(task.status)}
        <span class="mono">${esc(task.task_id)}</span>
        <span title="${esc(formatDateTime(task.created_at))}">${esc(relativeTime(task.created_at))}</span>
        ${task.status === "queued" && !selectedThreadArchived ? `<button class="ghost sm" data-task-action="cancel" data-task-id="${esc(task.task_id)}">Cancel</button>` : ""}
      </div>
      ${stream.join("")}
    </article>`;
}

async function sendMessage() {
  if (showingArchivedThreads || selectedThreadArchived) return;
  const message = $("new-task").value.trim();
  const runtime = $("new-task-runtime").value;
  const model = $("new-task-model").value;
  const effort = $("new-task-effort").value;
  if ((!message && !pendingAttachments.length) || !model || !effort || $("create-task").disabled) return;
  // A request without thread_id asks the backend to open a new thread with a
  // generated successive name (thread-1, thread-2, ...).
  const startingNewThread = selectedThreadId === null;
  const request = { input_message: "" };
  if (startingNewThread) Object.assign(request, { agent_runtime: runtime, model, effort });
  else request.thread_id = selectedThreadId;
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
  const result = await api("POST", "/messages", request);
  $("new-task").value = "";
  pendingAttachments = [];
  renderAttachments();
  autosizeComposer();
  if (startingNewThread) {
    // A brand-new thread has no prior event stream to keep; start its
    // newest-page accumulator clean so its first poll reads only this task.
    resetThreadEvents();
  }
  selectedThreadId = result.thread_id;
  if (startingNewThread) {
    selectedThreadName = result.thread_id;
    selectedThreadRuntime = result.agent_runtime;
    selectedThreadModel = result.model;
    selectedThreadEffort = result.effort;
  }
  forceScrollBottom = true;
  updateComposer();
  await refresh();
}

async function taskAction(button) {
  const taskId = button.dataset.taskId;
  const action = button.dataset.taskAction;
  if (action === "cancel") {
    await api("POST", `/tasks/${taskId}/cancel`);
    await refreshSelectedThread();
  }
}

async function stopRunningTask() {
  const runningTask = tasks.find(task => task.status === "running");
  if (!runningTask || !confirm("Stop running task " + runningTask.task_id + "?")) return;
  await api("POST", `/tasks/${runningTask.task_id}/kill`);
  await refreshSelectedThread();
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
  selectedThreadId = null;
  selectedThreadName = null;
  selectedThreadRuntime = null;
  selectedThreadModel = null;
  selectedThreadEffort = null;
  selectedThreadArchived = false;
  tasks = [];
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
      thread.dataset.archived === "true",
    ).catch(error => setStatus(error.message));
    return;
  }
  const taskButton = event.target.closest && event.target.closest("button[data-task-action]");
  if (taskButton) {
    taskAction(taskButton).catch(error => setStatus(error.message));
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
$("load-earlier").addEventListener("click", () => loadOlderThreadEvents().catch(error => setStatus(error.message)));
$("chat-scroll").addEventListener("scroll", () => {
  const scroller = $("chat-scroll");
  const movedUp = scroller.scrollTop < lastChatScrollTop;
  lastChatScrollTop = scroller.scrollTop;
  if (!movedUp || scroller.scrollTop > 160) return;
  loadOlderThreadEvents().catch(error => setStatus(error.message));
}, { passive: true });
$("stop-task").addEventListener("click", () => stopRunningTask().catch(error => setStatus(error.message)));
$("create-task").addEventListener("click", () => sendMessage().catch(error => setStatus(error.message)));
$("attach-file").addEventListener("click", () => attachFile().catch(error => setStatus(error.message)));
$("new-task-runtime").addEventListener("change", () => setSessionOptions());
$("new-task-model").addEventListener("change", () => setSessionOptions($("new-task-model").value));
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
  const active = threads.some(thread => (thread.active_tasks || []).length > 0);
  setTimeout(scheduleRefresh, active ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS);
}
scheduleRefresh();
