(() => {
"use strict";

let nextRequestId = 1;
const localFiles = new Map();
let threads = [];
// The selected thread's live status ("idle" | "running"), from the host
// through the thread index. Turns and their contents render purely from the
// event stream.
let selectedThreadStatus = "idle";
// The selected thread opens on its newest event page. Live events page
// forward from the newest cursor; earlier history is prepended on demand from
// the oldest cursor. Full activity and conversation-only views keep separate
// cursors, so hiding activity can page actual messages without losing the
// activity position needed if it is shown again.
let threadEvents = [];
let threadEventPages = freshThreadEventPages();
let loadingOlderThreadEvents = false;
let lastChatScrollTop = 0;
let keepComposerTailVisible = false;
let composerTailFrame = 0;
let openThreadAtTail = false;
// Keep each opened thread's loaded window for the lifetime of this mounted
// surface. A browser reload intentionally starts fresh.
const threadViewStates = new Map();
const EVENTS_PAGE = 6;
const INITIAL_EVENT_PAGES = 3;
const VIEW_STATE_LIMIT = 50;
const ACTIVE_REFRESH_MS = 1000;
const IDLE_REFRESH_MS = 5000;
const WORKSPACE_API_TIMEOUT_MS = 30000;
const REFRESH_ERROR_DISPLAY_DELAY_MS = 5000;
// Prefer a responsive composer over waiting through the full synchronous
// provider acknowledgement path. A timed-out request can still be accepted,
// so the error explicitly tells the operator that retrying may duplicate it.
const MESSAGE_DELIVERY_TIMEOUT_MS = 15 * 1000;
// Stop still waits through the proxy because duplicating it is not useful.
const AGENT_DELIVERY_TIMEOUT_MS = 60 * 1000;
const DELIVERY_TIMEOUT_MESSAGE = "Delivery is taking longer than expected. "
  + "You can try again; the message may be submitted twice.";
const COMPOSER_DRAFTS_STORAGE_KEY = "kern.agent-chat.composer-drafts.v1";
const COMPOSER_DRAFT_LIMIT = 50;
let selectedThreadId = null;
let selectedThreadName = null;
let selectedThreadRuntime = null;
let selectedThreadModel = null;
let selectedThreadEffort = null;
let selectedThreadArchived = false;
let showingArchivedThreads = false;
let showingActivity = false;
let activityToggleSequence = 0;
let sessionOptions = {};
let activeRuntimes = null;
// Captured on first render, before any "(not activated)" suffix is applied.
const RUNTIME_OPTION_LABELS = new Map();
let pendingAttachments = [];
let attachmentActivity = null;
let sendingMessage = false;
let sendingMessageThreadKey = null;
let composerContextSequence = 0;
let refreshSequence = 0;
let selectedRefreshSequence = 0;
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
let refreshErrorTimer = null;
let pendingRefreshErrorMessage = "";
let renameThreadReturnFocus = null;

const chatRoot = window.KernWorkspaceRoots.chat;
const $ = id => chatRoot.querySelector(`#${CSS.escape(id)}`);
const composerDrafts = loadComposerDrafts();
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

function loadComposerDrafts() {
  try {
    const drafts = JSON.parse(localStorage.getItem(COMPOSER_DRAFTS_STORAGE_KEY) || "{}");
    return drafts && typeof drafts === "object" && !Array.isArray(drafts) ? drafts : {};
  } catch (_error) {
    return {};
  }
}

function composerDraftKey(threadId = selectedThreadId) {
  return threadId === null ? "new" : `thread:${threadId}`;
}

function selectedThreadIsSending() {
  return sendingMessage && sendingMessageThreadKey === composerDraftKey();
}

function persistComposerDrafts() {
  try {
    localStorage.setItem(COMPOSER_DRAFTS_STORAGE_KEY, JSON.stringify(composerDrafts));
  } catch (_error) {
    // Draft persistence is best-effort when browser storage is unavailable.
  }
}

function saveComposerDraft() {
  const key = composerDraftKey();
  const value = $("new-task").value;
  delete composerDrafts[key];
  if (value) composerDrafts[key] = value;
  while (Object.keys(composerDrafts).length > COMPOSER_DRAFT_LIMIT) {
    delete composerDrafts[Object.keys(composerDrafts)[0]];
  }
  persistComposerDrafts();
}

function restoreComposerDraft() {
  $("new-task").value = composerDrafts[composerDraftKey()] || "";
  autosizeComposer();
}

function clearComposerDraft(threadId, submittedDraft) {
  const key = composerDraftKey(threadId);
  if ((composerDrafts[key] ?? "") !== submittedDraft) return false;
  delete composerDrafts[key];
  persistComposerDrafts();
  return true;
}

function api(method, path, body, timeoutMs = WORKSPACE_API_TIMEOUT_MS, timeoutMessage = "request timed out") {
  if (!path.startsWith("/")) throw new Error("Chat API path must be absolute");
  return Promise.race([
    window.KernHost.api(method, "/v1/workspace/chat" + path, body),
    new Promise((_, reject) => setTimeout(() => reject(new Error(timeoutMessage)), timeoutMs)),
  ]);
}

async function requestFileUpload(action, selectionId, maximumFiles) {
  if (action === "select") {
    const files = await window.KernHost.chooseFiles(maximumFiles || ATTACHMENT_LIMIT);
    if (files === null) return null;
    return { selections: files.map(file => {
      const selection_id = `chat-${nextRequestId++}`;
      localFiles.set(selection_id, file);
      return { selection_id, original_name: file.name, size_bytes: file.size };
    }) };
  }
  const file = localFiles.get(selectionId);
  if (!file) throw new Error("file selection is no longer available");
  if (action === "discard") {
    localFiles.delete(selectionId);
    return { discarded: true };
  }
  if (action !== "upload") throw new Error("file upload action is not allowed");
  const response = await window.KernHost.apiUpload(file);
  localFiles.delete(selectionId);
  return response;
}

async function requestHostCopy(text) {
  await navigator.clipboard.writeText(text);
  return { copied: true };
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

function deferRefreshError(message) {
  pendingRefreshErrorMessage = message;
  if (refreshErrorTimer !== null) return;
  refreshErrorTimer = setTimeout(() => {
    refreshErrorTimer = null;
    setStatus(pendingRefreshErrorMessage, "refresh");
  }, REFRESH_ERROR_DISPLAY_DELAY_MS);
}

function clearDeferredRefreshError() {
  if (refreshErrorTimer !== null) clearTimeout(refreshErrorTimer);
  refreshErrorTimer = null;
  pendingRefreshErrorMessage = "";
}

async function refresh() {
  const sequence = ++refreshSequence;
  const archivedView = showingArchivedThreads;
  try {
    // Re-read every refresh: connecting a provider from Home must reach an
    // already-mounted composer without a full page reload. The option matrix
    // itself is static, so only a change in activation is re-rendered, leaving
    // any model or effort the operator picked in the meantime alone.
    const firstLoad = !Object.keys(sessionOptions).length;
    const optionResponse = await api("GET", "/session-options");
    if (sequence !== refreshSequence || archivedView !== showingArchivedThreads) return;
    if (!optionResponse.session_options || typeof optionResponse.session_options !== "object") {
      throw new Error("Agent Chat returned invalid session options");
    }
    sessionOptions = optionResponse.session_options;
    const nextActive = Array.isArray(optionResponse.active_runtimes)
      ? optionResponse.active_runtimes
      : null;
    if (firstLoad) {
      activeRuntimes = nextActive;
      setSessionOptions(selectedThreadModel, selectedThreadEffort);
    } else if (JSON.stringify(nextActive) !== JSON.stringify(activeRuntimes)) {
      activeRuntimes = nextActive;
      if (applyRuntimeAvailability()) {
        // Availability moved us to a different runtime, so the model and
        // effort now belong to the runtime we left. Rebuild them rather than
        // letting a mismatched pair through to session validation.
        setSessionOptions();
      } else {
        updateComposerActions();
      }
    }
    // A successful /threads already proves the backend is up; when it is down
    // the proxy's own 502 ("workspaces backend unavailable") surfaces as the error.
    const response = await api("GET", archivedView ? "/threads?archived=true" : "/threads");
    if (sequence !== refreshSequence || archivedView !== showingArchivedThreads) return;
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
    } else if (selectedThreadId) {
      const removedThreadId = selectedThreadId;
      clearSelectedThread();
      if (window.location.hash === `#chat/${encodeURIComponent(removedThreadId)}`) {
        window.KernHost.navigateWorkspace("chat", null, true);
      }
    }
    renderThreads();
    window.dispatchEvent(new CustomEvent("kern-chat-updated", {
      detail: { threads, archived: showingArchivedThreads },
    }));
    if (selectedThreadId) {
      const refreshedThreadId = selectedThreadId;
      const rendered = await refreshSelectedThread();
      if (sequence !== refreshSequence || archivedView !== showingArchivedThreads) return;
      const visibleThread = threads.find(thread => thread.thread_id === refreshedThreadId);
      if (rendered && selectedThreadId === refreshedThreadId && visibleThread) {
        markSelectedThreadSeen(visibleThread);
      }
    }
    if (sequence !== refreshSequence || archivedView !== showingArchivedThreads) return;
    clearDeferredRefreshError();
    setStatus("", "refresh");
  } catch (error) {
    if (sequence === refreshSequence && archivedView === showingArchivedThreads) {
      deferRefreshError(error.message);
    }
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
  // Clearing is a write, so it follows the composer: hidden while the thread
  // is archived or the archived list is open, and refused while a turn runs.
  $("clear-memory").hidden = !hasThread || readOnly;
  $("clear-memory").disabled = running;
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
  const scroller = $("chat-scroll");
  const scrollerTop = scroller.getBoundingClientRect().top;
  const distanceFromBottom = scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight;
  const atTail = distanceFromBottom <= 1;
  const conversationEntries = Array.from(
    $("thread-detail").querySelectorAll(".thread-entry:not(.thread-activity)"),
  );
  // Prefer the first complete row in view. A clipped prompt can still cross
  // the viewport edge while the response below it is what the operator
  // deliberately aligned for reading; anchoring the clipped prompt would
  // move that response when the activity rows between them disappear.
  let anchor = conversationEntries.find(
    entry => entry.getBoundingClientRect().top >= scrollerTop,
  ) || conversationEntries.find(
    entry => entry.getBoundingClientRect().bottom > scrollerTop,
  );
  let anchorOffset = anchor
    ? anchor.getBoundingClientRect().top - scrollerTop
    : null;
  const previousScrollTop = scroller.scrollTop;
  showingActivity = !showingActivity;
  const toggleSequence = ++activityToggleSequence;
  updateActivityToggle();
  clearActivityAnchorSpace();
  // Hiding a long run can remove most of the scroll height at once. Preserve
  // what the operator was reading instead of letting the viewport jump to an
  // unrelated message. If the shorter conversation cannot support the same
  // scroll offset, temporary bottom space supplies only the missing range and
  // disappears when activity returns. Readers at the tail remain pinned to
  // the newest event without any spacer.
  if (atTail) {
    scroller.scrollTop = scroller.scrollHeight;
  } else if (anchor && anchorOffset !== null && anchor.isConnected) {
    preserveActivityAnchor(scroller, anchor, anchorOffset, scrollerTop);
  } else {
    scroller.scrollTop = Math.min(
      previousScrollTop,
      Math.max(0, scroller.scrollHeight - scroller.clientHeight),
    );
  }
  renderHistoryLoader();
  if (selectedThreadId && !activeThreadEventPage().initialized) {
    const threadId = selectedThreadId;
    refreshSelectedThread().then(() => {
      if (threadId !== selectedThreadId || toggleSequence !== activityToggleSequence) return;
      if (atTail) {
        scroller.scrollTop = scroller.scrollHeight;
      } else if (anchor && anchorOffset !== null && anchor.isConnected) {
        preserveActivityAnchor(scroller, anchor, anchorOffset, scrollerTop);
      }
    }).catch(error => {
      if (threadId === selectedThreadId) setStatus(error.message);
    });
  }
}

function preserveActivityAnchor(scroller, anchor, anchorOffset, scrollerTop) {
  const desiredScrollTop = scroller.scrollTop + (
    anchor.getBoundingClientRect().top - scrollerTop - anchorOffset
  );
  const maximumScrollTop = Math.max(0, scroller.scrollHeight - scroller.clientHeight);
  if (desiredScrollTop > maximumScrollTop) {
    $("thread-detail").style.setProperty(
      "--activity-anchor-space",
      `${Math.ceil(desiredScrollTop - maximumScrollTop)}px`,
    );
  }
  scroller.scrollTop = Math.max(0, desiredScrollTop);
}

function clearActivityAnchorSpace() {
  $("thread-detail").style.removeProperty("--activity-anchor-space");
}

// Two different questions. Null active runtimes means the host could not say,
// so neither gate applies: an unknown status must never hide a usable provider
// or block sending.

// Can it be shown as the selection? A thread keeps its recorded runtime here
// even after deactivation, so the composer still shows what it actually ran
// with instead of silently rewriting history.
function runtimeSelectable(runtime) {
  if (!Array.isArray(activeRuntimes)) return true;
  if (selectedThreadId !== null && runtime === selectedThreadRuntime) return true;
  return activeRuntimes.includes(runtime);
}

// Can the host actually run it? A deactivated runtime is refused on admission,
// so a recorded one gets no exemption here: displaying it is honest, offering
// to send on it is not.
function runtimeRunnable(runtime) {
  if (!Array.isArray(activeRuntimes)) return true;
  return activeRuntimes.includes(runtime);
}

function applyRuntimeAvailability() {
  const select = $("new-task-runtime");
  for (const option of select.options) {
    if (!RUNTIME_OPTION_LABELS.has(option.value)) {
      RUNTIME_OPTION_LABELS.set(option.value, option.textContent);
    }
    const available = runtimeSelectable(option.value);
    const label = RUNTIME_OPTION_LABELS.get(option.value);
    option.disabled = !available;
    option.textContent = available ? label : `${label} (not activated)`;
  }
  // The markup opens on the first runtime, and activation arrives later. A
  // deactivated selection cannot have come from the operator, so move to one
  // that works. When nothing is active there is nowhere to move to: keep the
  // selection and let updateComposerActions() hold Send closed rather than
  // pretending a deactivated runtime is usable.
  if (selectedThreadId === null && !runtimeRunnable(select.value)) {
    const usable = Object.keys(sessionOptions).find(runtimeRunnable);
    if (usable) {
      select.value = usable;
      // The model and effort still belong to the runtime we just left, so the
      // caller has to rebuild them before they can be sent.
      return true;
    }
  }
  return false;
}

function firstAvailableRuntime() {
  const offered = Object.keys(sessionOptions);
  return offered.find(runtimeRunnable) || offered[0] || "codex";
}

function setSessionOptions(preferredModel, preferredEffort) {
  applyRuntimeAvailability();
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
  // A deactivated runtime cannot run the message, so sending it would only
  // surface a host rejection after the fact.
  const hasSessionOption = Boolean($("new-task-model").value && $("new-task-effort").value)
    && runtimeRunnable($("new-task-runtime").value);
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
  const sendButton = $("create-task");
  const selectedSending = selectedThreadIsSending();
  sendButton.disabled = (
    activeBlock
    || sendingMessage
    || attachmentActivity !== null
    || hasOversizedAttachment
    || !hasSessionOption
  );
  sendButton.classList.toggle("sending", selectedSending);
  sendButton.setAttribute("aria-busy", String(selectedSending));
  sendButton.setAttribute("aria-label", selectedSending ? "Sending message" : "Send");
  sendButton.title = selectedSending ? "Sending message" : "Send (Enter)";
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
  saveComposerDraft();
  saveSelectedThreadView();
  activityToggleSequence += 1;
  clearActivityAnchorSpace();
  selectedRefreshSequence += 1;
  if (threadId !== selectedThreadId) composerContextSequence += 1;
  selectedThreadId = threadId;
  selectedThreadName = name;
  selectedThreadRuntime = runtime;
  selectedThreadModel = model;
  selectedThreadEffort = effort;
  selectedThreadStatus = status || "idle";
  selectedThreadArchived = archived;
  window.KernHost.navigateWorkspace("chat", threadId);
  restoreComposerDraft();
  loadSelectedSessionControls();
  restoreThreadView(threadId);
  openThreadAtTail = true;
  updateComposer();
  renderThreads();
  renderThreadHistory();
  const rendered = await refreshSelectedThread();
  if (rendered && selectedThreadId === threadId) {
    markSelectedThreadSeen({ thread_id: threadId });
  }
}

function markSelectedThreadSeen(thread) {
  const acknowledgedMessageSeq = threadEvents.reduce((latest, event) => (
    ["thread.message", "thread.memory_cleared"].includes(event.event_type)
      ? Math.max(latest, Number(event.seq) || 0)
      : latest
  ), 0);
  window.KernHost.markWorkspaceSeen("chat", {
    ...thread,
    latest_message_seq: acknowledgedMessageSeq,
  });
}

async function refreshSelectedThread() {
  if (!selectedThreadId) {
    renderThreadHistory();
    return false;
  }
  // Capture the id: a thread switch mid-flight must not let a stale response
  // land in the newly selected thread's state.
  const threadId = selectedThreadId;
  const sequence = ++selectedRefreshSequence;
  await refreshThreadEvents(threadId, sequence);
  if (threadId !== selectedThreadId || sequence !== selectedRefreshSequence) return false;
  updateComposer();
  renderThreadHistory();
  return true;
}

function saveSelectedThreadView() {
  if (!selectedThreadId) return;
  // Refresh insertion order so the oldest unvisited thread is evicted first.
  threadViewStates.delete(selectedThreadId);
  threadViewStates.set(selectedThreadId, {
    events: threadEvents,
    eventPages: {
      all: { ...threadEventPages.all },
      conversation: { ...threadEventPages.conversation },
    },
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
  threadEventPages = {
    all: { ...state.eventPages.all },
    conversation: { ...state.eventPages.conversation },
  };
  loadingOlderThreadEvents = false;
  lastChatScrollTop = 0;
  renderHistoryLoader();
}

function resetThreadEvents() {
  threadEvents = [];
  threadEventPages = freshThreadEventPages();
  loadingOlderThreadEvents = false;
  lastChatScrollTop = 0;
  renderHistoryLoader();
}

function freshThreadEventPages() {
  const page = () => ({
    oldestSeq: null,
    newestSeq: 0,
    initialized: false,
    hasOlder: false,
  });
  return { all: page(), conversation: page() };
}

function activeThreadEventPage() {
  return showingActivity
    ? threadEventPages.all
    : threadEventPages.conversation;
}

function threadEventPath(threadId, pageState, cursorName = null, cursor = null) {
  const conversationOnly = pageState === threadEventPages.conversation;
  const query = [];
  if (conversationOnly) query.push("activity=false");
  if (cursorName !== null) query.push(`${cursorName}=${cursor}`);
  const suffix = query.length ? `?${query.join("&")}` : "";
  return `/threads/${encodeURIComponent(threadId)}/events${suffix}`;
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

function hasWorkingMemoryBoundary() {
  return threadEvents.some(event => event.event_type === "thread.memory_cleared");
}

function visibleThreadEvents() {
  for (let index = threadEvents.length - 1; index >= 0; index -= 1) {
    if (threadEvents[index].event_type === "thread.memory_cleared") {
      return threadEvents.slice(index);
    }
  }
  return threadEvents;
}

async function refreshThreadEvents(threadId, refreshSequence) {
  const pageState = activeThreadEventPage();
  if (!pageState.initialized) {
    // No cursor means "latest page". Prefetch three bounded pages so a first
    // view normally has enough history to scroll. With activity hidden, the
    // backend fills those pages with conversation events rather than making
    // the operator page through command-output deltas that cannot be seen.
    const response = await api(
      "GET",
      threadEventPath(threadId, pageState),
    );
    if (threadId !== selectedThreadId || refreshSequence !== selectedRefreshSequence) return;
    const events = response.events || [];
    mergeThreadEvents(events);
    if (events.length) {
      pageState.oldestSeq = events[0].seq;
      pageState.newestSeq = events[events.length - 1].seq;
    }
    let oldestPage = events;
    for (
      let page = 1;
      page < INITIAL_EVENT_PAGES
      && oldestPage.length === EVENTS_PAGE
      && pageState.oldestSeq !== null
      && !hasWorkingMemoryBoundary();
      page += 1
    ) {
      const before = pageState.oldestSeq;
      const olderResponse = await api(
        "GET",
        threadEventPath(threadId, pageState, "before", before),
      );
      if (threadId !== selectedThreadId || refreshSequence !== selectedRefreshSequence) return;
      oldestPage = (olderResponse.events || []).filter(event => event.seq < before);
      if (oldestPage.length) {
        mergeThreadEvents(oldestPage);
        pageState.oldestSeq = oldestPage[0].seq;
      }
    }
    pageState.hasOlder = oldestPage.length === EVENTS_PAGE;
    pageState.initialized = true;
    renderHistoryLoader();
    return;
  }
  // Each view advances its own tail. Switching activity back on catches the
  // full stream up from its prior cursor without discarding messages loaded
  // through the conversation-only lane.
  for (;;) {
    const response = await api(
      "GET",
      threadEventPath(threadId, pageState, "since", pageState.newestSeq),
    );
    if (threadId !== selectedThreadId || refreshSequence !== selectedRefreshSequence) return;
    const events = response.events || [];
    // Only accept events past the cursor, so a server that re-sends an
    // overlapping page can never double-append into the stream.
    const fresh = events.filter(event => event.seq > pageState.newestSeq);
    if (fresh.length) {
      mergeThreadEvents(fresh);
      pageState.newestSeq = fresh[fresh.length - 1].seq;
      if (pageState.oldestSeq === null) pageState.oldestSeq = fresh[0].seq;
    }
    // Keep paging only while the cursor advanced by a full page. A short or
    // no-progress page means the live tail is caught up.
    if (fresh.length < EVENTS_PAGE) return;
  }
}

async function loadOlderThreadEvents() {
  const pageState = activeThreadEventPage();
  if (
    !selectedThreadId
    || !pageState.initialized
    || !pageState.hasOlder
    || hasWorkingMemoryBoundary()
    || loadingOlderThreadEvents
    || pageState.oldestSeq === null
  ) return;
  const threadId = selectedThreadId;
  const refreshSequence = selectedRefreshSequence;
  const before = pageState.oldestSeq;
  loadingOlderThreadEvents = true;
  renderHistoryLoader();
  try {
    const response = await api(
      "GET",
      threadEventPath(threadId, pageState, "before", before),
    );
    if (threadId !== selectedThreadId || refreshSequence !== selectedRefreshSequence) return;
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
      pageState.oldestSeq = older[0].seq;
    }
    pageState.hasOlder = older.length === EVENTS_PAGE;
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
  const pageState = activeThreadEventPage();
  loader.hidden = !selectedThreadId || !pageState.hasOlder || hasWorkingMemoryBoundary();
  loader.dataset.oldestSeq = pageState.oldestSeq === null
    ? ""
    : String(pageState.oldestSeq);
  const button = $("load-earlier");
  button.disabled = loadingOlderThreadEvents;
  button.textContent = loadingOlderThreadEvents ? "Loading earlier messages…" : "Load earlier messages";
}

function renderThreadHistory() {
  const key = JSON.stringify([
    selectedThreadId,
    showingArchivedThreads,
    selectedThreadStatus,
    threadEventPages,
    threadEvents.length,
  ]);
  if (key === renderedHistoryKey) {
    if (openThreadAtTail) {
      const scroller = $("chat-scroll");
      scroller.scrollTop = scroller.scrollHeight;
      openThreadAtTail = false;
    }
    return;
  }
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
  const ordered = visibleThreadEvents().filter(event => (
    ["thread.message", "thread.activity", "thread.error", "thread.stopped",
      "thread.memory_cleared"].includes(event.event_type)
  ));
  if (switched || !ordered.length) {
    renderedEntryHtml.clear();
    detail.innerHTML = ordered.length
      ? ""
      : `<div class="chat-hero"><p>No retained messages for this thread yet.</p></div>`;
  }
  const openActivities = new Set(
    Array.from(chatRoot.querySelectorAll(".activity-card[open]"))
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
  // Every explicit open lands on the latest message. Keep each thread's
  // loaded event window across switches, but do not restore an old reading
  // position that makes the operator hunt for new replies.
  if (openThreadAtTail || switched) scroller.scrollTop = scroller.scrollHeight;
  else if (forceScrollBottom) scroller.scrollTo({ top: scroller.scrollHeight, behavior: "smooth" });
  else if (nearBottom) scroller.scrollTop = scroller.scrollHeight;
  openThreadAtTail = false;
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
    return `<article class="thread-entry thread-activity" data-entry-id="${entryId}">
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
  if (event.event_type === "thread.memory_cleared") {
    // Rendered from the host text rather than fixed wording here, so the
    // notice has one source of truth. A boundary notice, not an activity
    // entry: it must read the same when activity is hidden, and carrying no
    // activity payload keeps repeated clears out of activity compaction.
    const notice = typeof payload.message === "string" && payload.message
      ? payload.message
      : "Working memory cleared.";
    return `<article class="thread-entry thread-stopped" data-entry-id="${entryId}">
      ${esc(notice)}
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
  sendingMessageThreadKey = composerDraftKey();
  updateComposerActions();
  try {
    await sendMessageUnlocked();
  } finally {
    sendingMessage = false;
    sendingMessageThreadKey = null;
    updateComposerActions();
  }
}

async function sendMessageUnlocked() {
  if (showingArchivedThreads || selectedThreadArchived) return;
  const submittedDraft = $("new-task").value;
  saveComposerDraft();
  const message = submittedDraft.trim();
  const runtime = $("new-task-runtime").value;
  const model = $("new-task-model").value;
  const effort = $("new-task-effort").value;
  if ((!message && !pendingAttachments.length) || !model || !effort) return;
  setStatus("");
  // A request without thread_id asks the backend to open a new thread with a
  // generated successive name (thread-1, thread-2, ...).
  const startingNewThread = selectedThreadId === null;
  const submittedThreadId = selectedThreadId;
  const submittedComposerContext = composerContextSequence;
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
  const result = await api(
    "POST",
    "/messages",
    request,
    MESSAGE_DELIVERY_TIMEOUT_MS,
    DELIVERY_TIMEOUT_MESSAGE,
  );
  // Any list poll that started before acceptance can still describe the
  // thread as idle. Fence it before committing the authoritative running
  // state below; the explicit refresh starts a new generation.
  refreshSequence += 1;
  const clearedSubmittedDraft = clearComposerDraft(submittedThreadId, submittedDraft);
  const stillViewingSubmittedThread = selectedThreadId === submittedThreadId;
  const stillViewingSubmittedContext = (
    stillViewingSubmittedThread
    && composerContextSequence === submittedComposerContext
  );
  if (
    clearedSubmittedDraft
    && stillViewingSubmittedThread
    && $("new-task").value === submittedDraft
  ) {
    $("new-task").value = "";
  }
  pendingAttachments = [];
  renderAttachments();
  autosizeComposer();
  if (startingNewThread && stillViewingSubmittedContext) {
    // A brand-new thread has no prior event stream to keep; start its
    // newest-page accumulator clean so its first poll reads only this work.
    resetThreadEvents();
  }
  if (stillViewingSubmittedContext) {
    selectedThreadId = result.thread_id;
    if (startingNewThread) selectedThreadName = result.thread_id;
    selectedThreadRuntime = runtime;
    selectedThreadModel = model;
    selectedThreadEffort = effort;
    selectedThreadStatus = "running";
    forceScrollBottom = true;
    if (startingNewThread && window.location.hash === "#chat/new") {
      window.KernHost.navigateWorkspace("chat", result.thread_id, true);
    }
  }
  updateComposer();
  // Acceptance is the send boundary. Do not keep the composer spinner active
  // while the thread body and sidebar catch up in the background.
  void Promise.all([refresh(), window.KernHost.refreshNavigation()]).catch(error => {
    deferRefreshError(error.message);
  });
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
  await Promise.all([refresh(), window.KernHost.refreshNavigation()]);
}

async function clearWorkingMemory() {
  if (!selectedThreadId) return;
  // The host refuses a clear on a running thread; say so here rather than
  // surfacing a 409 the operator cannot act on.
  if (selectedThreadStatus === "running") {
    setStatus("Stop the agent before clearing working memory.");
    return;
  }
  if (!confirm(
    "Clear working memory?\n\n"
    + "The agent starts fresh from here. Earlier messages will be hidden "
    + "and will no longer be sent to it."
  )) return;
  await api("POST", `/threads/${encodeURIComponent(selectedThreadId)}/clear-memory`);
  // The marker is the only confirmation, and a clear made while scrolled up
  // would otherwise append it below the viewport and look like nothing
  // happened. Scroll to the boundary the same way a send scrolls to its
  // message.
  forceScrollBottom = true;
  await refresh();
}

async function setSelectedThreadArchived() {
  if (!selectedThreadId) return;
  const threadId = selectedThreadId;
  const operationRoute = window.location.hash;
  const action = selectedThreadArchived ? "unarchive" : "archive";
  // A running turn keeps going after archiving and the archived view hides
  // Stop, so refuse to archive until the turn ends.
  if (action === "archive" && selectedThreadStatus === "running") {
    setStatus("Stop the agent before archiving this thread.");
    return;
  }
  await api("POST", `/threads/${encodeURIComponent(threadId)}/${action}`);
  if (selectedThreadId === threadId && window.location.hash === operationRoute) {
    clearSelectedThread();
    window.KernHost.navigateWorkspace("chat");
  }
  await Promise.all([refresh(), window.KernHost.refreshNavigation()]);
}

function setRenameThreadOpen(open) {
  const overlay = $("rename-thread-overlay");
  if (open) {
    if (!selectedThreadId) return;
    renameThreadReturnFocus = chatRoot.activeElement || $("rename-thread");
    $("rename-thread-input").value = selectedThreadName || selectedThreadId;
    $("rename-thread-error").hidden = true;
    overlay.hidden = false;
    requestAnimationFrame(() => $("rename-thread-input").select());
    return;
  }
  overlay.hidden = true;
  $("rename-thread-save").disabled = false;
  if (renameThreadReturnFocus && renameThreadReturnFocus.isConnected) renameThreadReturnFocus.focus();
  renameThreadReturnFocus = null;
}

async function renameSelectedThread() {
  if (!selectedThreadId) return;
  const threadId = selectedThreadId;
  const name = $("rename-thread-input").value.trim();
  if (!name) {
    $("rename-thread-error").textContent = "Enter a thread name.";
    $("rename-thread-error").hidden = false;
    $("rename-thread-input").focus();
    return;
  }
  $("rename-thread-save").disabled = true;
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
  setRenameThreadOpen(false);
  await window.KernHost.refreshNavigation();
}

function clearSelectedThread() {
  saveComposerDraft();
  saveSelectedThreadView();
  activityToggleSequence += 1;
  clearActivityAnchorSpace();
  selectedRefreshSequence += 1;
  composerContextSequence += 1;
  selectedThreadId = null;
  selectedThreadName = null;
  selectedThreadRuntime = null;
  selectedThreadModel = null;
  selectedThreadEffort = null;
  selectedThreadStatus = "idle";
  selectedThreadArchived = false;
  restoreComposerDraft();
  resetThreadEvents();
  updateComposer();
  renderThreadHistory();
  renderThreads();
}

function startNewThread() {
  showingArchivedThreads = false;
  clearSelectedThread();
  // A new thread is unconfigured, so its composer opens on the first offered
  // configuration. Leaving the selectors as the previously opened thread left
  // them would start the thread's session on a runtime and model the operator
  // never chose. The reset belongs here rather than on the in-panel button
  // because the host navigation starts threads through the same function.
  $("new-task-runtime").value = firstAvailableRuntime();
  setSessionOptions();
  window.KernHost.navigateWorkspace("chat");
}

async function toggleArchivedThreads() {
  showingArchivedThreads = !showingArchivedThreads;
  clearSelectedThread();
  window.KernHost.navigateWorkspace("chat");
  await refresh();
}

// Must match the drawer breakpoint in agent_chat.css.
const drawerMedia = window.matchMedia("(max-width: 720px)");

function setSidebarOpen(open, restoreFocus = false) {
  const mobile = drawerMedia.matches;
  const isOpen = mobile && open;
  const pane = chatRoot.querySelector(".thread-pane");
  $("chat-app").classList.toggle("sidebar-open", isOpen);
  // The closed drawer is only moved off-canvas by a transform, so drop it
  // (and, while open, the pane behind it) from the tab order the same way
  // the host mobile nav does.
  pane.inert = mobile && !isOpen;
  chatRoot.querySelector(".chat-main").inert = isOpen;
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

function keepLatestMessageAboveComposer() {
  if (!keepComposerTailVisible) return;
  cancelAnimationFrame(composerTailFrame);
  composerTailFrame = requestAnimationFrame(() => {
    if (!keepComposerTailVisible) return;
    const scroller = $("chat-scroll");
    scroller.scrollTop = scroller.scrollHeight;
    lastChatScrollTop = scroller.scrollTop;
  });
}

chatRoot.addEventListener("click", event => {
  const fileButton = event.target.closest && event.target.closest(".md-open-file");
  if (fileButton) {
    window.KernHost.openAgentFile(
      fileButton.dataset.filePath || "",
      fileButton.dataset.fallbackPath || "",
    )
      .catch(error => setStatus(error.message));
    return;
  }
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
  startNewThread();
  $("new-task").focus();
});
$("archived-toggle").addEventListener("click", () => toggleArchivedThreads().catch(error => setStatus(error.message)));
$("rename-thread").addEventListener("click", () => setRenameThreadOpen(true));
$("rename-thread-close").addEventListener("click", () => setRenameThreadOpen(false));
$("rename-thread-cancel").addEventListener("click", () => setRenameThreadOpen(false));
$("rename-thread-backdrop").addEventListener("click", () => setRenameThreadOpen(false));
$("rename-thread-form").addEventListener("submit", event => {
  event.preventDefault();
  renameSelectedThread().catch(error => {
    $("rename-thread-save").disabled = false;
    $("rename-thread-error").textContent = error.message;
    $("rename-thread-error").hidden = false;
  });
});
$("clear-memory").addEventListener("click", () => clearWorkingMemory().catch(error => setStatus(error.message)));
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
$("chat-scroll").addEventListener("pointerdown", () => {
  keepComposerTailVisible = false;
}, { passive: true });
$("chat-scroll").addEventListener("wheel", () => {
  keepComposerTailVisible = false;
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
$("new-task").addEventListener("input", () => {
  autosizeComposer();
  keepLatestMessageAboveComposer();
  saveComposerDraft();
});
$("new-task").addEventListener("focus", () => {
  const scroller = $("chat-scroll");
  keepComposerTailVisible = (
    scroller.scrollHeight - scroller.scrollTop - scroller.clientHeight < 60
  );
  keepLatestMessageAboveComposer();
});
$("new-task").addEventListener("blur", () => {
  keepComposerTailVisible = false;
  cancelAnimationFrame(composerTailFrame);
});
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", keepLatestMessageAboveComposer, { passive: true });
}
$("new-task").addEventListener("keydown", event => {
  const sendKey = event.key === "Enter" && !event.isComposing && (!event.shiftKey || event.metaKey || event.ctrlKey);
  if (!sendKey) return;
  event.preventDefault();
  sendMessage().catch(error => setStatus(error.message));
});
chatRoot.addEventListener("keydown", event => {
  if (event.key !== "Escape" || $("rename-thread-overlay").hidden) return;
  event.preventDefault();
  setRenameThreadOpen(false);
});
$("sidebar-open").addEventListener("click", () => setSidebarOpen(true));
$("sidebar-close").addEventListener("click", () => setSidebarOpen(false, true));
$("sidebar-backdrop").addEventListener("click", () => setSidebarOpen(false, true));
drawerMedia.addEventListener("change", () => setSidebarOpen(false));

setSessionOptions();
restoreComposerDraft();
updateComposer();
renderThreadHistory();
renderAttachments();
setSidebarOpen(false);
window.KernChat = {
  newThread(prompt = "") {
    showingArchivedThreads = false;
    startNewThread();
    // An unsent draft is cheap to lose and the starter prompt is what the
    // operator just asked for, so replace it without interrupting them.
    if (prompt && $("new-task").value !== prompt) {
      $("new-task").value = prompt;
      saveComposerDraft();
      autosizeComposer();
      updateComposerActions();
    }
    $("new-task").focus();
  },
  openThread(thread) {
    showingArchivedThreads = Boolean(thread.archived);
    return showThread(
      thread.thread_id,
      thread.name,
      thread.agent_runtime,
      thread.model,
      thread.effort,
      thread.status,
      Boolean(thread.archived),
    );
  },
  refresh,
};
async function scheduleRefresh() {
  await refresh();
  const active = threads.some(thread => thread.status === "running");
  setTimeout(scheduleRefresh, active ? ACTIVE_REFRESH_MS : IDLE_REFRESH_MS);
}
scheduleRefresh();
})();
