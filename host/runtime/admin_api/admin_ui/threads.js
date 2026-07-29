// Read-only agent session view: thread list plus the selected thread's turn
// event log, paged from the thread event stream.

import { api } from "./api.js";
import { $, badge, esc, formatDateTime, runtimeLabel, setHtml } from "./helpers.js";

const EVENT_PAGE_LIMIT = 100;
const THREAD_LIST_PAGE_LIMIT = 100;

let threads = [];
let selectedThreadId = null, selectedThreadRuntime = null;
let threadEvents = [];
let threadEventsOldestSeq = null;
let threadEventsNewestSeq = 0;
let threadEventsInitialized = false;
let hasOlderThreadEvents = false;
let earlierThreadEventsRequest = null;

export async function loadThreads() {
  const loaded = [];
  const seenBefore = new Set();
  let before = null;
  while (true) {
    const query = new URLSearchParams({ limit: String(THREAD_LIST_PAGE_LIMIT) });
    if (before !== null) query.set("before", before);
    const listed = await api("GET", `/v1/threads?${query}`);
    loaded.push(...(listed.threads || []));
    const nextBefore = typeof listed.next_before === "string" && listed.next_before
      ? listed.next_before
      : null;
    if (nextBefore === null) break;
    if (seenBefore.has(nextBefore)) throw new Error("thread list pagination returned a repeated cursor");
    seenBefore.add(nextBefore);
    before = nextBefore;
  }
  threads = loaded;
  renderThreads();
}

function renderThreads() {
  if (!threads.length) {
    setHtml($("threads"), `<div class="empty-state">No retained sessions yet.</div>`);
    return;
  }
  setHtml($("threads"), threads.map(thread => `
    <button class="thread-item${thread.thread_id === selectedThreadId ? " selected" : ""}"
            data-action="show-thread" data-thread-id="${esc(thread.thread_id)}" data-runtime="${esc(thread.agent_runtime)}">
      <span class="thread-name">${esc(thread.thread_id)}</span>
      <span class="thread-meta">${esc(runtimeLabel(thread.agent_runtime))}
        ${thread.status === "running" ? badge(thread.status) : ""}</span>
      <span class="thread-meta">${esc(formatDateTime(thread.last_used_at))}</span>
    </button>`).join(""));
}

export async function showThread(threadId, agentRuntime) {
  if (threadId !== selectedThreadId) resetThreadEvents();
  selectedThreadId = threadId;
  selectedThreadRuntime = agentRuntime;
  renderThreads();
  await refreshSelectedThread();
}

function resetThreadEvents() {
  threadEvents = [];
  threadEventsOldestSeq = null;
  threadEventsNewestSeq = 0;
  threadEventsInitialized = false;
  hasOlderThreadEvents = false;
  earlierThreadEventsRequest = null;
}

function mergeThreadEvents(events) {
  const bySeq = new Map(threadEvents.map(event => [event.seq, event]));
  for (const event of events) bySeq.set(event.seq, event);
  threadEvents = Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
  if (threadEvents.length) {
    threadEventsOldestSeq = threadEvents[0].seq;
    threadEventsNewestSeq = threadEvents[threadEvents.length - 1].seq;
  }
}

export async function refreshSelectedThread() {
  if (selectedThreadId === null) { renderThreadHistory(); return; }
  const threadId = selectedThreadId;
  if (!threadEventsInitialized) {
    // No cursor means "latest page"; earlier history loads on demand.
    const response = await api(
      "GET", `/v1/threads/${encodeURIComponent(threadId)}/events`
    );
    if (threadId !== selectedThreadId) return;
    const events = response.events || [];
    mergeThreadEvents(events);
    hasOlderThreadEvents = events.length === EVENT_PAGE_LIMIT;
    threadEventsInitialized = true;
  } else {
    const response = await api(
      "GET",
      `/v1/threads/${encodeURIComponent(threadId)}/events?since=${threadEventsNewestSeq}`
    );
    if (threadId !== selectedThreadId) return;
    mergeThreadEvents((response.events || []).filter(event => event.seq > threadEventsNewestSeq));
  }
  renderThreadHistory();
}

export async function loadEarlierThreadEvents() {
  if (
    selectedThreadId === null
    || !threadEventsInitialized
    || !hasOlderThreadEvents
    || threadEventsOldestSeq === null
    || earlierThreadEventsRequest !== null
  ) return;
  const threadId = selectedThreadId;
  const before = threadEventsOldestSeq;
  const request = { threadId, before };
  earlierThreadEventsRequest = request;
  try {
    const response = await api(
      "GET",
      `/v1/threads/${encodeURIComponent(threadId)}/events?before=${before}`
    );
    if (threadId !== selectedThreadId) return;
    const older = (response.events || []).filter(event => event.seq < before);
    mergeThreadEvents(older);
    hasOlderThreadEvents = older.length === EVENT_PAGE_LIMIT;
    renderThreadHistory();
  } finally {
    if (earlierThreadEventsRequest === request) earlierThreadEventsRequest = null;
  }
}

export function renderThreadHistory() {
  if (selectedThreadId === null) {
    setHtml($("thread-detail"), `
      <div class="thread-head">
        <span class="thread-title">Agent session log</span>
      </div>
      <div class="empty-state thread-empty">Select a session to inspect its retained turn events.</div>`);
    return;
  }
  const thread = threads.find(item => item.thread_id === selectedThreadId);
  setHtml($("thread-detail"), `
    <div class="thread-head">
      <span class="thread-title">${esc(selectedThreadId)}</span>
      <span class="muted">${esc(runtimeLabel(selectedThreadRuntime))}</span>
      ${thread && thread.status === "running" ? badge(thread.status) : ""}
    </div>
    ${hasOlderThreadEvents ? `<div class="actions"><button class="ghost sm" data-action="load-earlier-thread-events">Load earlier events</button></div>` : ""}
    ${threadEvents.length ? threadEventsHtml()
      : `<div class="empty-state thread-empty">No retained events for this session yet.</div>`}`);
}

function threadEventsHtml() {
  return `
    <div class="table-scroll"><table>
      <tr><th>seq</th><th>time</th><th>type</th><th>source</th><th>payload</th></tr>
      ${threadEvents.map(event => `
        <tr>
          <td>${esc(event.seq)}</td>
          <td class="muted time">${esc(formatDateTime(event.timestamp))}</td>
          <td>${esc(event.event_type)}</td>
          <td>${esc(event.payload.source || "")}</td>
          <td><pre>${esc(event.payload.message || event.payload.error_message || JSON.stringify(event.payload))}</pre></td>
        </tr>`).join("")}
    </table></div>`;
}
