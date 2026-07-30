"use strict";

const APP_ID = "personal_web_app_builder";
const MAX_WORKER_MESSAGES_PER_SECOND = 100;
const MAX_WORKER_MESSAGES_PER_TURN = 128;
const MAX_WORKER_MUTATIONS_PER_TURN = 16;
const WORKER_TURN_TIMEOUT_MS = 3000;
const MAX_RENDER_HTML_BYTES = 128 * 1024;
const MAX_RENDER_CSS_BYTES = 64 * 1024;
const MAX_CSS_CONDITION_BYTES = 512;
const MAX_AGENT_MESSAGE_BYTES = 4000;
const MAX_EVENT_FIELDS = 64;
const MAX_EVENT_FIELD_BYTES = 8192;
const MAX_EVENT_PAYLOAD_BYTES = 64 * 1024;
const MAX_DATA_VALUE_BYTES = 256 * 1024;
const CONVERSATION_EVENTS_PAGE = 6;
const INITIAL_CONVERSATION_EVENT_PAGES = 3;
const VIEW_STATE_LIMIT = 50;
const ATTACHMENT_LIMIT = 10;
const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
const APP_API_TIMEOUT_MS = 12000;
// The outer browser-to-app proxy can wait 50s for a synchronous provider
// acknowledgement. Sends and Stop must outlive that hop or a retry can
// duplicate a message the host accepted after the frame gave up.
const AGENT_DELIVERY_TIMEOUT_MS = 60 * 1000;
const COMPOSER_DRAFTS_STORAGE_KEY = "kern.agentic-web-app.composer-drafts.v1";
const COMPOSER_DRAFT_LIMIT = 50;
const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();
const pendingApi = new Map();
let requestCounter = 0;
let sessionOptions = {};
let apps = [];
let showingArchivedApps = false;
let selectedAppId = null;
let selectedAppName = null;
let selectedAppArchived = false;
let renderedAppsKey = "";
let snapshot = { app: null, session: null, status: "idle" };
let conversationEvents = [];
let conversationEventsOldestSeq = null;
let conversationEventsNewestSeq = 0;
let conversationEventsInitialized = false;
let hasOlderConversationEvents = false;
let loadingOlderConversationEvents = false;
let lastChatScrollTop = 0;
let restoredChatScrollTop = null;
const conversationViewStates = new Map();
const renderedChatEntries = new Map();
let renderedRevision = -1;
let generatedRoot = null;
let workerRun = null;
let appsRefreshSequence = 0;
const messageBusyApps = new Set();
let selectedRefreshSequence = 0;
let establishedSession = null;
let establishedSessionKey = "";
let pendingAttachments = [];
const attachmentActivities = new Map();

const $ = id => document.getElementById(id);
const composerDrafts = loadComposerDrafts();

function loadComposerDrafts() {
  try {
    const drafts = JSON.parse(localStorage.getItem(COMPOSER_DRAFTS_STORAGE_KEY) || "{}");
    return drafts && typeof drafts === "object" && !Array.isArray(drafts) ? drafts : {};
  } catch (_error) {
    return {};
  }
}

function persistComposerDrafts() {
  try {
    localStorage.setItem(COMPOSER_DRAFTS_STORAGE_KEY, JSON.stringify(composerDrafts));
  } catch (_error) {
    // Draft persistence is best-effort when browser storage is unavailable.
  }
}

function saveComposerDraft() {
  if (!selectedAppId) return;
  const key = `app:${selectedAppId}`;
  const value = $("message").value;
  delete composerDrafts[key];
  if (value) composerDrafts[key] = value;
  while (Object.keys(composerDrafts).length > COMPOSER_DRAFT_LIMIT) {
    delete composerDrafts[Object.keys(composerDrafts)[0]];
  }
  persistComposerDrafts();
}

function restoreComposerDraft() {
  $("message").value = selectedAppId
    ? composerDrafts[`app:${selectedAppId}`] || ""
    : "";
}

function clearComposerDraft(threadId, submittedDraft) {
  const key = `app:${threadId}`;
  if ((composerDrafts[key] ?? "") !== submittedDraft) return false;
  delete composerDrafts[key];
  persistComposerDrafts();
  return true;
}

window.addEventListener("message", event => {
  const message = event.data;
  if (
    event.source !== parent
    || !message
    || ![
      "kern-app-api-result",
      "kern-app-copy-text-result",
      "kern-app-upload-file-result",
    ].includes(message.type)
  ) return;
  const pending = pendingApi.get(message.request_id);
  if (!pending) return;
  pendingApi.delete(message.request_id);
  if (message.ok) pending.resolve(message.cancelled ? null : message.body);
  else pending.reject(new Error(message.error || "App request failed"));
});

function api(method, path, body, timeoutMs = APP_API_TIMEOUT_MS) {
  const requestId = `pwa-${Date.now()}-${++requestCounter}`;
  return new Promise((resolve, reject) => {
    pendingApi.set(requestId, { resolve, reject });
    parent.postMessage({
      type: "kern-app-api",
      request_id: requestId,
      method,
      path: `/v1/apps/${APP_ID}/api${path}`,
      body,
    }, "*");
    setTimeout(() => {
      if (!pendingApi.has(requestId)) return;
      pendingApi.delete(requestId);
      reject(new Error("App request timed out"));
    }, timeoutMs);
  });
}

function requestHostCopy(text) {
  const requestId = `pwa-copy-${Date.now()}-${++requestCounter}`;
  return new Promise((resolve, reject) => {
    pendingApi.set(requestId, { resolve, reject });
    parent.postMessage({
      type: "kern-app-copy-text",
      request_id: requestId,
      text,
    }, "*");
    setTimeout(() => {
      if (!pendingApi.has(requestId)) return;
      pendingApi.delete(requestId);
      reject(new Error("Copy timed out"));
    }, 10000);
  });
}

function requestFileUpload(action, selectionId, maximumFiles) {
  const requestId = `pwa-file-${Date.now()}-${++requestCounter}`;
  return new Promise((resolve, reject) => {
    pendingApi.set(requestId, { resolve, reject });
    parent.postMessage({
      type: "kern-app-upload-file",
      request_id: requestId,
      action,
      ...(selectionId ? { selection_id: selectionId } : {}),
      ...(maximumFiles ? { max_files: maximumFiles } : {}),
    }, "*");
    setTimeout(() => {
      if (!pendingApi.has(requestId)) return;
      pendingApi.delete(requestId);
      reject(new Error("File operation timed out"));
    }, 5 * 60 * 1000);
  });
}

function capabilityWorkerBootstrap(maxRenderHtmlBytes, maxRenderCssBytes) {
  "use strict";
  // The frame CSP is authoritative. Function.prototype.constructor can still
  // recover the real Function constructor and WebAssembly remains available,
  // so script-src must keep unsafe-eval and wasm-unsafe-eval absent. The blob
  // worker inherits that policy and connect-src 'none'. Scrubbing common
  // globals below is defense in depth, not the code-execution or egress bound.
  const send = globalThis.postMessage.bind(globalThis);
  const clone = globalThis.structuredClone.bind(globalThis);
  const resolvePromise = Promise.resolve.bind(Promise);
  const encodeText = TextEncoder.prototype.encode.bind(new TextEncoder());
  const denied = () => { throw new Error("This capability is not available"); };
  const deniedGlobals = [
    "fetch", "XMLHttpRequest", "WebSocket", "EventSource", "RTCPeerConnection",
    "webkitRTCPeerConnection", "Worker", "SharedWorker", "importScripts",
    "WebSocketStream", "WebTransport", "FontFace", "BroadcastChannel", "indexedDB",
    "caches", "navigator", "eval", "Function",
  ];
  for (const name of deniedGlobals) {
    let target = globalThis;
    while (target) {
      const descriptor = Object.getOwnPropertyDescriptor(target, name);
      if (descriptor && descriptor.configurable) delete target[name];
      target = Object.getPrototypeOf(target);
    }
    try {
      Object.defineProperty(globalThis, name, {
        value: denied, writable: false, configurable: false, enumerable: false,
      });
    } catch (_error) {
      // CSP is still the authoritative network and code-loading boundary if
      // a browser exposes a non-configurable compatibility property.
    }
  }

  let durableData = {};
  let requestId = 0;
  let loadHandler = null;
  const handlers = new Map();
  const pending = new Map();
  const actionName = value => {
    if (typeof value !== "string" || !/^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) {
      throw new TypeError("action must be a bounded name");
    }
    return value;
  };
  const mutation = (action, path, value, includeValue) => new Promise((resolve, reject) => {
    const id = `mutation-${++requestId}`;
    pending.set(id, { resolve, reject });
    const message = { type: "data-action", request_id: id, action, path: clone(path) };
    if (includeValue) message.value = clone(value);
    send(message);
  });
  const api = Object.freeze({
    onLoad(handler) {
      if (typeof handler !== "function") throw new TypeError("handler must be a function");
      loadHandler = handler;
    },
    on(action, handler) {
      action = actionName(action);
      if (typeof handler !== "function") throw new TypeError("handler must be a function");
      handlers.set(action, handler);
    },
    data() { return clone(durableData); },
    render(html, css = "") {
      if (typeof html !== "string" || typeof css !== "string") {
        throw new TypeError("render content must be strings");
      }
      if (encodeText(html).length > maxRenderHtmlBytes || encodeText(css).length > maxRenderCssBytes) {
        throw new RangeError("render content exceeds its encoded size limit");
      }
      send({ type: "render", html, css });
    },
    set(path, value) { return mutation("set", path, value, true); },
    delete(path) { return mutation("delete", path, undefined, false); },
    append(path, value) { return mutation("append", path, value, true); },
    askAgent(message) { send({ type: "agent-request", message }); },
    notify(message, level = "info") { send({ type: "notify", message, level }); },
  });
  Object.defineProperty(globalThis, "app", {
    value: api, writable: false, configurable: false, enumerable: true,
  });
  Object.defineProperty(globalThis, "postMessage", {
    value: denied, writable: false, configurable: false, enumerable: false,
  });

  globalThis.addEventListener("message", event => {
    const message = event.data;
    if (!message || typeof message !== "object") return;
    if (message.type === "init") {
      durableData = clone(message.data);
      resolvePromise()
        .then(() => message.load && loadHandler ? loadHandler() : undefined)
        .then(() => send({ type: "initialized" }))
        .catch(() => send({ type: "initialization-error" }));
      return;
    }
    if (message.type === "data-result") {
      const waiter = pending.get(message.request_id);
      if (!waiter) return;
      pending.delete(message.request_id);
      if (message.ok) {
        durableData = clone(message.data);
        waiter.resolve(clone(durableData));
      } else {
        waiter.reject(new Error("Data update failed"));
      }
      return;
    }
    if (message.type === "event") {
      const handler = handlers.get(message.action);
      resolvePromise(handler ? handler(clone(message.event)) : undefined)
        .then(() => send({ type: "turn-complete", turn_id: message.turn_id }))
        .catch(() => send({ type: "turn-error", turn_id: message.turn_id }));
    }
  });
  send({ type: "ready" });
}

const allowedElements = new Set([
  "ABBR", "ADDRESS", "ARTICLE", "ASIDE", "BDI", "BDO", "BLOCKQUOTE", "BR",
  "BUTTON", "CAPTION", "CITE", "CODE", "DATA", "DATALIST", "DD", "DEL",
  "DETAILS", "DFN", "DIV", "DL", "DT", "EM", "FIELDSET", "FIGCAPTION",
  "FIGURE", "FOOTER", "FORM", "H1", "H2", "H3", "H4", "H5", "H6", "HEADER",
  "HR", "I", "INPUT", "INS", "KBD", "LABEL", "LEGEND", "LI", "MAIN", "MARK",
  "MENU", "METER", "NAV", "OL", "OPTGROUP", "OPTION", "OUTPUT", "P", "PRE",
  "PROGRESS", "Q", "RP", "RT", "RUBY", "S", "SAMP", "SEARCH", "SECTION",
  "SELECT", "SMALL", "SPAN", "STRONG", "SUB", "SUMMARY", "SUP", "TABLE",
  "TBODY", "TD", "TEXTAREA", "TFOOT", "TH", "THEAD", "TIME", "TR", "U", "UL",
  "VAR", "WBR",
]);
const droppedElements = new Set([
  "A", "AUDIO", "BASE", "EMBED", "IFRAME", "IMG", "LINK", "META", "OBJECT",
  "PICTURE", "SCRIPT", "SOURCE", "STYLE", "TRACK", "VIDEO",
]);
const globalAttributes = new Set([
  "id", "class", "title", "hidden", "role", "lang", "spellcheck",
]);
const safeAttributes = new Set([
  "abbr", "checked", "cols", "datetime", "disabled", "for", "headers", "high",
  "inputmode", "label", "list", "low", "max", "maxlength", "min", "minlength",
  "multiple", "name", "open", "optimum", "pattern", "placeholder", "readonly",
  "required", "reversed", "rows", "scope", "selected", "size", "start", "step",
  "value", "wrap",
]);
const allowedInputTypes = new Set([
  "checkbox", "color", "date", "datetime-local", "email", "month", "number", "radio",
  "range", "search", "tel", "text", "time", "week",
]);
const allowedCssProperties = new Set(`
  accent-color align-content align-items align-self animation animation-delay
  animation-direction animation-duration animation-fill-mode animation-iteration-count
  animation-name animation-play-state animation-timing-function appearance aspect-ratio
  backdrop-filter background background-attachment background-blend-mode background-clip
  background-color background-image background-origin background-position
  background-position-x background-position-y background-repeat background-size block-size
  border border-block border-block-color border-block-end
  border-block-start border-bottom border-bottom-color border-bottom-left-radius
  border-bottom-right-radius border-bottom-style border-bottom-width border-collapse
  border-color border-inline border-inline-color border-inline-end border-inline-start
  border-left border-left-color border-left-style border-left-width border-radius border-right
  border-right-color border-right-style border-right-width border-spacing border-style
  border-top border-top-color border-top-left-radius border-top-right-radius border-top-style
  border-top-width border-width bottom box-shadow box-sizing caret-color clear clip clip-path
  color color-scheme column-gap columns content counter-increment counter-reset counter-set cursor
  direction display filter flex flex-basis flex-direction flex-flow flex-grow flex-shrink
  flex-wrap float font-family font-feature-settings font-kerning font-optical-sizing font-size
  font-stretch font-style font-variant font-variation-settings font-weight gap
  grid grid-area grid-auto-columns grid-auto-flow grid-auto-rows grid-column grid-column-end
  grid-column-gap grid-column-start grid-gap grid-row grid-row-end grid-row-gap grid-row-start
  grid-template grid-template-areas grid-template-columns grid-template-rows height hyphens
  inline-size inset inset-block inset-block-end inset-block-start inset-inline inset-inline-end
  inset-inline-start isolation justify-content justify-items justify-self left letter-spacing
  line-height list-style list-style-position list-style-type margin margin-block margin-block-end
  margin-block-start margin-bottom margin-inline margin-inline-end margin-inline-start margin-left
  margin-right margin-top max-block-size max-height max-inline-size max-width min-block-size
  min-height min-inline-size min-width mix-blend-mode object-fit object-position opacity order
  outline outline-color outline-offset outline-style outline-width overflow overflow-anchor
  overflow-clip-margin overflow-wrap overflow-x overflow-y overscroll-behavior
  overscroll-behavior-block overscroll-behavior-inline overscroll-behavior-x
  overscroll-behavior-y padding padding-block
  padding-block-end padding-block-start padding-bottom padding-inline padding-inline-end
  padding-inline-start padding-left padding-right padding-top place-content place-items place-self
  pointer-events position quotes resize right rotate row-gap scale scroll-behavior scroll-margin
  scroll-margin-block scroll-margin-block-end scroll-margin-block-start scroll-margin-bottom
  scroll-margin-inline scroll-margin-inline-end scroll-margin-inline-start scroll-margin-left
  scroll-margin-right scroll-margin-top scroll-padding scroll-padding-block scroll-padding-block-end
  scroll-padding-block-start scroll-padding-bottom scroll-padding-inline scroll-padding-inline-end
  scroll-padding-inline-start scroll-padding-left scroll-padding-right scroll-padding-top
  scroll-snap-align scroll-snap-stop scroll-snap-type scrollbar-color scrollbar-gutter
  scrollbar-width tab-size table-layout text-align text-decoration text-decoration-color
  text-decoration-line text-decoration-style text-decoration-thickness text-emphasis
  text-emphasis-color text-emphasis-position text-emphasis-style text-indent text-overflow
  text-rendering text-shadow text-transform text-underline-offset text-wrap text-wrap-mode
  text-wrap-style top
  touch-action transform transform-origin transition transition-delay transition-duration
  transition-property transition-timing-function translate user-select vertical-align visibility
  white-space white-space-collapse width word-break word-spacing word-wrap writing-mode z-index
`.trim().split(/\s+/));
const safeCustomProperty = /^--[A-Za-z][A-Za-z0-9_-]{0,63}$/;
const forbiddenCssValue = /url\s*\(|\bimage\s*\(|image-set\s*\(|cross-fade\s*\(|element\s*\(|paint\s*\(|src\s*\(/i;

function sanitizeHtml(html) {
  if (typeof html !== "string" || textEncoder.encode(html).length > MAX_RENDER_HTML_BYTES) {
    throw new Error("Generated HTML is invalid or too large");
  }
  const template = document.createElement("template");
  template.innerHTML = html;
  const output = document.createDocumentFragment();
  for (const node of template.content.childNodes) cloneSafeNode(node, output);
  return output;
}

function cloneSafeNode(node, parent) {
  if (node.nodeType === Node.TEXT_NODE) {
    parent.append(document.createTextNode(node.data));
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  if (droppedElements.has(node.tagName)) return;
  if (!allowedElements.has(node.tagName)) {
    for (const child of node.childNodes) cloneSafeNode(child, parent);
    return;
  }
  const clean = document.createElement(node.tagName.toLowerCase());
  for (const attribute of node.attributes) copySafeAttribute(node, clean, attribute.name, attribute.value);
  if (node.tagName === "BUTTON") clean.type = "button";
  if (node.tagName === "INPUT") {
    const type = node.getAttribute("type") || "text";
    clean.type = allowedInputTypes.has(type.toLowerCase()) ? type.toLowerCase() : "text";
  }
  for (const child of node.childNodes) cloneSafeNode(child, clean);
  parent.append(clean);
}

function copySafeAttribute(source, target, name, value) {
  const lower = name.toLowerCase();
  if (globalAttributes.has(lower)) {
    target.setAttribute(lower, value.slice(0, 512));
    return;
  }
  if (lower.startsWith("aria-") && /^[a-z-]{1,40}$/.test(lower)) {
    target.setAttribute(lower, value.slice(0, 512));
    return;
  }
  if (lower === "dir" && ["auto", "ltr", "rtl"].includes(value.toLowerCase())) {
    target.setAttribute(lower, value.toLowerCase());
    return;
  }
  if (lower === "tabindex" && ["-1", "0"].includes(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (lower === "data-action" && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (lower === "data-field" && /^[A-Za-z][A-Za-z0-9_.-]{0,63}$/.test(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (safeAttributes.has(lower)) target.setAttribute(lower, value.slice(0, 512));
  if ((lower === "colspan" || lower === "rowspan") && /^[1-9][0-9]?$/.test(value)) {
    target.setAttribute(lower, value);
  }
}

function sanitizeCss(css) {
  if (typeof css !== "string" || textEncoder.encode(css).length > MAX_RENDER_CSS_BYTES) {
    throw new Error("Generated CSS is invalid or too large");
  }
  const sheet = new CSSStyleSheet();
  sheet.replaceSync(css);
  return Array.from(sheet.cssRules, sanitizeRule).filter(Boolean).join("\n");
}

function sanitizeRule(rule) {
  const kind = rule.constructor.name;
  if (kind === "CSSStyleRule") {
    // Shadow CSS can otherwise restyle its host and escape the generated
    // canvas. Reject escapes too, so an encoded :host cannot bypass the check.
    if (rule.selectorText.includes("\\") || /:host(?:-context)?(?:\b|\()/i.test(rule.selectorText)) return "";
    return `${rule.selectorText}{${sanitizeDeclarations(rule.style)}}`;
  }
  if (kind === "CSSMediaRule") {
    if (textEncoder.encode(rule.conditionText).length > MAX_CSS_CONDITION_BYTES) return "";
    return `@media ${rule.conditionText}{${Array.from(rule.cssRules, sanitizeRule).filter(Boolean).join("")}}`;
  }
  if (kind === "CSSKeyframesRule") {
    const name = /^[A-Za-z_][A-Za-z0-9_-]{0,63}$/.test(rule.name) ? rule.name : "generated";
    return `@keyframes ${name}{${Array.from(rule.cssRules, child => `${child.keyText}{${sanitizeDeclarations(child.style)}}`).join("")}}`;
  }
  return "";
}

function sanitizeDeclarations(style) {
  const safe = [];
  for (const property of style) {
    const normalized = property.toLowerCase();
    if (!allowedCssProperties.has(normalized) && !safeCustomProperty.test(property)) continue;
    const value = style.getPropertyValue(property);
    if (textEncoder.encode(value).length > 4096 || value.includes("\\") || forbiddenCssValue.test(value)) continue;
    const priority = style.getPropertyPriority(property) === "important" ? "!important" : "";
    safe.push(`${normalized}:${value}${priority}`);
  }
  return safe.join(";");
}

function renderGenerated(html, css) {
  const fragment = sanitizeHtml(html);
  const safeCss = sanitizeCss(css);
  const host = $("generated-host");
  if (!generatedRoot) generatedRoot = host.attachShadow({ mode: "open" });
  generatedRoot.replaceChildren();
  const style = document.createElement("style");
  style.textContent = `:host{display:block;min-height:100%;color:var(--text);background:var(--bg);font-family:system-ui,sans-serif}${safeCss}`;
  generatedRoot.append(style, fragment);
  host.hidden = false;
  $("empty-state").hidden = true;
}

function clearGenerated() {
  generatedRoot.replaceChildren();
  $("generated-host").hidden = true;
  $("empty-state").hidden = false;
}

function syncEmptyState() {
  const hasApp = selectedAppId !== null;
  if (!hasApp) {
    $("first-run-how").hidden = true;
    $("empty-title").textContent = showingArchivedApps ? "Archived apps" : "Select an app";
    $("empty-description").textContent = showingArchivedApps
      ? "Choose an archived app to view it, or return to active apps."
      : "Choose an app from the list, or create a new workspace.";
    $("empty-primary").textContent = showingArchivedApps ? "Show active" : "New app";
    return;
  }
  const firstRun = !snapshot.session;
  $("first-run-how").hidden = !firstRun;
  $("empty-title").textContent = selectedAppArchived
    ? "This app is archived"
    : firstRun ? "Build this app" : "Your app will appear here";
  $("empty-description").textContent = selectedAppArchived
    ? "Unarchive it to keep building or use its interactive controls."
    : firstRun
      ? "Open agent chat and describe what you want. The agent can create the interface, behavior, and structured data."
      : "Open agent chat to continue building or ask the agent to create the first version.";
  $("empty-primary").textContent = selectedAppArchived
    ? "Unarchive"
    : firstRun ? "Start building" : "Open agent chat";
}

function eventPayload(element) {
  const fields = Object.create(null);
  for (const field of Array.from(generatedRoot.querySelectorAll("[data-field]")).slice(0, MAX_EVENT_FIELDS)) {
    const key = field.dataset.field;
    if (field.type === "checkbox" || field.type === "radio") fields[key] = Boolean(field.checked);
    else fields[key] = clipEncodedText(String(field.value || ""), MAX_EVENT_FIELD_BYTES);
  }
  const payload = {
    action: element.dataset.action,
    value: "value" in element ? clipEncodedText(String(element.value || ""), MAX_EVENT_FIELD_BYTES) : "",
    checked: "checked" in element ? Boolean(element.checked) : false,
    fields,
  };
  if (jsonByteLength(payload) > MAX_EVENT_PAYLOAD_BYTES) {
    return { action: element.dataset.action, value: "", checked: false, fields: {} };
  }
  return payload;
}

function clipEncodedText(value, limit) {
  const encoded = textEncoder.encode(value);
  if (encoded.length <= limit) return value;
  let clipped = textDecoder.decode(encoded.slice(0, limit));
  while (textEncoder.encode(clipped).length > limit) clipped = clipped.slice(0, -1);
  return clipped;
}

function jsonByteLength(value) {
  try {
    const encoded = JSON.stringify(value);
    return encoded === undefined ? -1 : textEncoder.encode(encoded).length;
  } catch (_error) {
    return -1;
  }
}

function generatedInteraction(event) {
  if (!selectedAppId || selectedAppArchived) return;
  if (!(event.target instanceof Element)) return;
  const changeControl = event.target.closest("input, select, textarea");
  if ((event.type === "click" && changeControl) || (event.type === "change" && !changeControl)) return;
  const target = event.target.closest("[data-action]");
  if (!target || !generatedRoot.contains(target)) return;
  event.preventDefault();
  if (workerRun) {
    showRuntimeStatus("Finishing the previous app action");
    return;
  }
  runCapabilityWorker({ action: target.dataset.action, event: eventPayload(target) });
}

async function runCapabilityWorker(pendingEvent = null) {
  if (!selectedAppId || selectedAppArchived || !snapshot.app || !snapshot.app.javascript) return;
  if (workerRun) workerRun.finish("restarted");
  const threadId = selectedAppId;
  const app = snapshot.app;
  const source = (
    `(${capabilityWorkerBootstrap.toString()})(${MAX_RENDER_HTML_BYTES},${MAX_RENDER_CSS_BYTES});\n`
    + `${app.javascript}\n`
  );
  const url = URL.createObjectURL(new Blob([source], { type: "application/javascript" }));
  const worker = new Worker(url);
  URL.revokeObjectURL(url);
  const run = {
    worker,
    threadId,
    data: app.data,
    revision: app.revision,
    state: "starting",
    event: pendingEvent,
    count: 0,
    totalMessages: 0,
    mutations: 0,
    mutationPending: false,
    agentRequested: false,
    windowStarted: performance.now(),
    timer: null,
    finish(reason) {
      clearTimeout(this.timer);
      worker.terminate();
      if (workerRun === this) workerRun = null;
      if (reason === "timeout" || reason === "error") showRuntimeStatus("Generated behavior stopped safely", "error");
    },
  };
  workerRun = run;
  run.timer = setTimeout(() => run.finish("timeout"), WORKER_TURN_TIMEOUT_MS);
  worker.addEventListener("error", event => {
    event.preventDefault();
    run.finish("error");
  });
  worker.addEventListener("message", event => handleWorkerMessage(run, event.data));
}

async function handleWorkerMessage(run, message) {
  if (
    workerRun !== run || selectedAppId !== run.threadId || selectedAppArchived
    || !message || typeof message !== "object"
  ) return;
  const now = performance.now();
  if (now - run.windowStarted >= 1000) {
    run.windowStarted = now;
    run.count = 0;
  }
  if (++run.count > MAX_WORKER_MESSAGES_PER_SECOND || ++run.totalMessages > MAX_WORKER_MESSAGES_PER_TURN) {
    run.finish("error");
    return;
  }
  if (message.type === "ready" && run.state === "starting") {
    run.state = "initializing";
    run.worker.postMessage({ type: "init", data: run.data, load: !run.event });
    return;
  }
  if (message.type === "initialization-error" && run.state === "initializing") {
    run.finish("error");
    return;
  }
  if (message.type === "initialized" && run.state === "initializing") {
    if (!run.event) {
      run.finish("complete");
      return;
    }
    run.state = "event";
    run.worker.postMessage({
      type: "event", action: run.event.action, event: run.event.event, turn_id: "turn",
    });
    return;
  }
  if ((message.type === "turn-complete" || message.type === "turn-error") && run.state === "event" && message.turn_id === "turn") {
    run.finish(message.type === "turn-complete" ? "complete" : "error");
    return;
  }
  if (message.type === "render" && typeof message.html === "string" && typeof message.css === "string") {
    try { renderGenerated(message.html, message.css); }
    catch (_error) { run.finish("error"); }
    return;
  }
  if (message.type === "notify") {
    if (typeof message.message !== "string" || textEncoder.encode(message.message).length > 1000) return;
    if (!["info", "success", "error"].includes(message.level)) return;
    showRuntimeStatus(message.message, message.level);
    return;
  }
  if (message.type === "agent-request") {
    if (
      run.state !== "event" || run.agentRequested || typeof message.message !== "string"
      || !message.message.trim()
      || textEncoder.encode(message.message).length > MAX_AGENT_MESSAGE_BYTES
    ) return;
    run.agentRequested = true;
    void sendMessage(message.message.trim(), run.threadId);
    return;
  }
  if (message.type === "data-action") await handleWorkerDataAction(run, message);
}

async function handleWorkerDataAction(run, message) {
  if (
    run.state !== "event" || run.mutationPending
    || run.mutations >= MAX_WORKER_MUTATIONS_PER_TURN
    || typeof message.request_id !== "string"
    || !/^mutation-[1-9][0-9]{0,8}$/.test(message.request_id)
    || !["set", "delete", "append"].includes(message.action)
    || !validDataPath(message.path)
    || (message.action !== "delete" && jsonByteLength(message.value) > MAX_DATA_VALUE_BYTES)
    || (message.action !== "delete" && jsonByteLength(message.value) < 0)
  ) {
    run.finish("error");
    return;
  }
  run.mutationPending = true;
  run.mutations += 1;
  const body = {
    action: message.action,
    expected_revision: run.revision,
    path: message.path,
  };
  if (message.action !== "delete") body.value = message.value;
  try {
    const response = await api(
      "POST",
      `/apps/${encodeURIComponent(run.threadId)}/runtime/actions`,
      body,
    );
    if (workerRun !== run || selectedAppId !== run.threadId) return;
    snapshot.app = response.app;
    run.data = response.app.data;
    run.revision = response.app.revision;
    renderedRevision = response.app.revision;
    $("revision-label").textContent = `Revision ${response.app.revision}`;
    run.mutationPending = false;
    run.worker.postMessage({ type: "data-result", request_id: message.request_id, ok: true, data: response.app.data });
  } catch (_error) {
    if (workerRun !== run) return;
    run.worker.postMessage({ type: "data-result", request_id: message.request_id, ok: false });
    await refreshSelectedApp(run.threadId);
  }
}

function validDataPath(path) {
  if (!Array.isArray(path) || !path.length || path.length > 16) return false;
  return path.every(segment => (
    Number.isInteger(segment) && segment >= 0
  ) || (
    typeof segment === "string" && segment.length > 0
    && textEncoder.encode(segment).length <= 128
  ));
}

function showRuntimeStatus(message, level = "info") {
  const status = $("runtime-status");
  status.textContent = message;
  status.className = `runtime-status ${level}`;
  status.hidden = false;
  setTimeout(() => { if (status.textContent === message) status.hidden = true; }, 4500);
}

function openChat() {
  if (!selectedAppId) return;
  $("chat-drawer").hidden = false;
  $("open-chat").setAttribute("aria-expanded", "true");
  if (!selectedAppArchived) $("message").focus();
}

function closeChat() {
  $("chat-drawer").hidden = true;
  $("open-chat").setAttribute("aria-expanded", "false");
}

function showChatStatus(message, error = false) {
  const status = $("chat-status");
  status.textContent = message;
  status.className = error ? "chat-status error" : "chat-status";
  status.hidden = !message;
}

function renderAttachments() {
  const container = $("attachments");
  const currentActivity = attachmentActivities.get(selectedAppId) || null;
  container.replaceChildren();
  for (const attachment of pendingAttachments) {
    const tooLarge = attachment.size_bytes > ATTACHMENT_MAX_BYTES;
    const chip = document.createElement("div");
    chip.className = `attachment${tooLarge ? " invalid" : ""}`;
    const name = document.createElement("span");
    name.className = "attachment-name";
    name.title = attachment.original_name;
    name.textContent = attachment.original_name;
    chip.append(name);
    if (tooLarge) {
      const error = document.createElement("span");
      error.className = "attachment-error";
      error.textContent = "25 MiB max";
      chip.append(error);
    }
    const remove = document.createElement("button");
    remove.className = "attachment-clear";
    remove.dataset.removeAttachment = attachment.selection_id;
    remove.ariaLabel = `Remove ${attachment.original_name}`;
    remove.title = `Remove ${attachment.original_name}`;
    remove.disabled = currentActivity !== null;
    remove.textContent = "×";
    chip.append(remove);
    container.append(chip);
  }
  if (currentActivity !== null) {
    const activity = document.createElement("div");
    activity.className = "attachment activity";
    activity.textContent = currentActivity;
    container.append(activity);
  }
  container.hidden = currentActivity === null && !pendingAttachments.length;
  setSessionOptions();
}

function setAttachmentActivity(threadId, activity) {
  if (activity === null) attachmentActivities.delete(threadId);
  else attachmentActivities.set(threadId, activity);
  renderAttachments();
}

async function attachFile() {
  const threadId = selectedAppId;
  const remaining = ATTACHMENT_LIMIT - pendingAttachments.length;
  if (
    !threadId
    || remaining <= 0
    || selectedAppArchived
    || messageBusyApps.has(threadId)
  ) return;
  showChatStatus("");
  setAttachmentActivity(threadId, "Selecting file…");
  try {
    const response = await requestFileUpload("select", null, remaining);
    if (response === null) return;
    if (!Array.isArray(response.selections) || !response.selections.length) {
      throw new Error("File selection returned an invalid response");
    }
    for (const selection of response.selections) {
      if (
        typeof selection.selection_id !== "string"
        || typeof selection.original_name !== "string"
        || typeof selection.size_bytes !== "number"
      ) {
        throw new Error("File selection returned an invalid response");
      }
    }
    if (selectedAppId !== threadId) {
      for (const selection of response.selections) {
        void requestFileUpload("discard", selection.selection_id).catch(() => {});
      }
      return;
    }
    if (pendingAttachments.length + response.selections.length > ATTACHMENT_LIMIT) {
      throw new Error(`You can attach up to ${ATTACHMENT_LIMIT} files.`);
    }
    pendingAttachments.push(...response.selections);
  } finally {
    setAttachmentActivity(threadId, null);
  }
}

async function removeAttachment(selectionId) {
  const index = pendingAttachments.findIndex(
    attachment => attachment.selection_id === selectionId,
  );
  if (index < 0) return;
  const [attachment] = pendingAttachments.splice(index, 1);
  renderAttachments();
  if (!attachment.file) await requestFileUpload("discard", attachment.selection_id);
}

function clearPendingAttachments() {
  // A send captures the current array before switching apps. Leave its
  // selections alive so that in-flight upload can finish for the app it was
  // submitted from; the new app still starts with a fresh array below.
  if (!selectedAppId || !messageBusyApps.has(selectedAppId)) {
    for (const attachment of pendingAttachments) {
      if (!attachment.file) {
        void requestFileUpload("discard", attachment.selection_id).catch(() => {});
      }
    }
  }
  pendingAttachments = [];
  if (selectedAppId) attachmentActivities.delete(selectedAppId);
  renderAttachments();
}

async function sendMessage(forcedMessage = null, targetAppId = null) {
  const fromGeneratedApp = forcedMessage !== null;
  const threadId = targetAppId || selectedAppId;
  const submittedDraft = fromGeneratedApp ? null : $("message").value;
  const message = (fromGeneratedApp ? forcedMessage : submittedDraft).trim();
  const attachments = fromGeneratedApp ? [] : pendingAttachments;
  if (
    (!message && !attachments.length)
    || !threadId
    || threadId !== selectedAppId
    || selectedAppArchived
  ) return;
  if (messageBusyApps.has(threadId)) {
    if (fromGeneratedApp) showRuntimeStatus("Agent is already starting");
    return;
  }
  if (!fromGeneratedApp) saveComposerDraft();
  messageBusyApps.add(threadId);
  setSessionOptions();
  showChatStatus("");
  if (fromGeneratedApp) showRuntimeStatus("Sending to agent…");
  try {
    for (const [index, attachment] of attachments.entries()) {
      if (attachment.file) continue;
      setAttachmentActivity(
        threadId,
        `Uploading ${index + 1} of ${attachments.length}…`,
      );
      const response = await requestFileUpload("upload", attachment.selection_id);
      if (
        !response.file
        || typeof response.file.path !== "string"
        || typeof response.file.name !== "string"
      ) {
        throw new Error("File upload returned an invalid response");
      }
      attachment.file = response.file;
    }
    setAttachmentActivity(threadId, null);
    const fileReferences = attachments
      .map(attachment => `[User-uploaded file: ${attachment.file.path}]`)
      .join("\n");
    const content = attachments.length
      ? `${message || (attachments.length === 1
        ? "Please review the uploaded file."
        : "Please review the uploaded files.")}\n\n${fileReferences}`
      : message;
    const body = { content };
    if (
      !snapshot.session
      || (!fromGeneratedApp && sessionConfigurationChanged())
    ) {
      body.agent_runtime = $("runtime").value;
      body.model = $("model").value;
      body.effort = $("effort").value;
    }
    const resource = fromGeneratedApp ? "runtime/agent-requests" : "messages";
    await api(
      "POST",
      `/apps/${encodeURIComponent(threadId)}/${resource}`,
      body,
      AGENT_DELIVERY_TIMEOUT_MS,
    );
    if (!fromGeneratedApp && selectedAppId === threadId) {
      const clearedSubmittedDraft = clearComposerDraft(threadId, submittedDraft);
      if (clearedSubmittedDraft && $("message").value === submittedDraft) {
        $("message").value = "";
      }
      pendingAttachments = [];
      renderAttachments();
    } else if (!fromGeneratedApp) {
      clearComposerDraft(threadId, submittedDraft);
    }
    await refreshSelectedApp(threadId);
    if (selectedAppId !== threadId) return;
    showChatStatus("");
    if (fromGeneratedApp) showRuntimeStatus("Sent to agent", "success");
  } catch (error) {
    setAttachmentActivity(threadId, null);
    if (selectedAppId !== threadId) {
      for (const attachment of attachments) {
        if (!attachment.file) {
          void requestFileUpload("discard", attachment.selection_id).catch(() => {});
        }
      }
      return;
    }
    showChatStatus(error.message || "Could not send to the agent", true);
    if (fromGeneratedApp) showRuntimeStatus("Could not send to the agent", "error");
  } finally {
    messageBusyApps.delete(threadId);
    if (selectedAppId === threadId) setSessionOptions();
  }
}

async function stopRunningTurn() {
  const threadId = selectedAppId;
  if (!threadId || snapshot.status !== "running" || selectedAppArchived) return;
  if (!confirm("Stop the agent?")) return;
  showChatStatus("Stopping…");
  try {
    await api(
      "POST",
      `/apps/${encodeURIComponent(threadId)}/stop`,
      {},
      AGENT_DELIVERY_TIMEOUT_MS,
    );
  } finally {
    if (selectedAppId === threadId) showChatStatus("");
  }
  await refreshSelectedApp(threadId);
}

function renderChat() {
  const history = $("chat-history");
  const entries = $("chat-turns");
  const nearBottom = history.scrollHeight - history.scrollTop - history.clientHeight < 48;
  const ordered = conversationEntries();
  renderHistoryLoader();
  if (!ordered.length) {
    renderedChatEntries.clear();
    entries.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "chat-empty";
    empty.textContent = selectedAppArchived
      ? "This archived app has no conversation yet."
      : "Describe the app you want. The agent can build its UI, behavior, and data, then keep changing it here.";
    entries.append(empty);
    syncAgentSettings(snapshot.session);
    return;
  }
  if (entries.firstElementChild?.classList.contains("chat-empty")) entries.replaceChildren();
  ordered.forEach((entry, index) => {
    const renderedKey = JSON.stringify(entry);
    const current = entries.children[index];
    if (!current || current.dataset.entryId !== entry.key) {
      entries.insertBefore(renderChatEntry(entry), current || null);
    } else if (renderedChatEntries.get(entry.key) !== renderedKey) {
      entries.replaceChild(renderChatEntry(entry), current);
    }
    renderedChatEntries.set(entry.key, renderedKey);
  });
  while (entries.children.length > ordered.length) {
    entries.lastElementChild.remove();
  }
  const visibleEntryKeys = new Set(ordered.map(entry => entry.key));
  for (const entryKey of renderedChatEntries.keys()) {
    if (!visibleEntryKeys.has(entryKey)) renderedChatEntries.delete(entryKey);
  }
  syncAgentSettings(snapshot.session);
  if (restoredChatScrollTop !== null) {
    history.scrollTop = restoredChatScrollTop;
    lastChatScrollTop = restoredChatScrollTop;
    restoredChatScrollTop = null;
  } else if (nearBottom) {
    history.scrollTop = history.scrollHeight;
    lastChatScrollTop = history.scrollTop;
  }
}

function conversationEntries() {
  const entries = [];
  for (const event of conversationEvents) {
    const payload = event.payload || {};
    if (event.event_type === "thread.error") {
      entries.push({
        key: `event-${event.seq}`,
        kind: "error",
        message: payload.error_message || "The agent stopped because of an error.",
      });
    } else if (event.event_type === "thread.stopped") {
      entries.push({
        key: `event-${event.seq}`,
        kind: "stopped",
        message: "Agent stopped",
      });
    } else if (
      event.event_type === "thread.message"
      && typeof payload.message === "string"
      && payload.message
    ) {
      entries.push({
        key: `event-${event.seq}`,
        kind: payload.source === "user" ? "user" : "agent",
        message: payload.message,
      });
    }
  }
  return entries;
}

function renderChatEntry(entryData) {
  const entry = document.createElement("article");
  entry.className = `chat-entry chat-${entryData.kind}`;
  entry.dataset.entryId = entryData.key;
  if (entryData.kind === "agent") {
    entry.classList.add("md-content");
    entry.innerHTML = KernRichText.renderMarkdown(entryData.message);
  } else {
    entry.textContent = entryData.message;
  }
  return entry;
}


function mergeConversationEvents(events) {
  const bySeq = new Map(conversationEvents.map(event => [event.seq, event]));
  // This compact conversation renders only lifecycle and text messages.
  // Tool/activity deltas are intentionally omitted instead of accumulating
  // an unbounded stream the UI never displays.
  for (const event of events) {
    if (event.event_type !== "thread.activity") bySeq.set(event.seq, event);
  }
  conversationEvents = Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
}

async function refreshConversationEvents(threadId, refreshSequence) {
  if (!conversationEventsInitialized) {
    const response = await api(
      "GET",
      `/apps/${encodeURIComponent(threadId)}/conversation/events`,
    );
    if (selectedAppId !== threadId || selectedRefreshSequence !== refreshSequence) return;
    const events = response.events || [];
    mergeConversationEvents(events);
    if (events.length) {
      conversationEventsOldestSeq = events[0].seq;
      conversationEventsNewestSeq = events[events.length - 1].seq;
    }
    let oldestPage = events;
    for (
      let page = 1;
      page < INITIAL_CONVERSATION_EVENT_PAGES
      && oldestPage.length === CONVERSATION_EVENTS_PAGE
      && conversationEventsOldestSeq !== null;
      page += 1
    ) {
      const before = conversationEventsOldestSeq;
      const olderResponse = await api(
        "GET",
        `/apps/${encodeURIComponent(threadId)}/conversation/events?before=${before}`,
      );
      if (selectedAppId !== threadId || selectedRefreshSequence !== refreshSequence) return;
      oldestPage = (olderResponse.events || []).filter(event => event.seq < before);
      if (oldestPage.length) {
        mergeConversationEvents(oldestPage);
        conversationEventsOldestSeq = oldestPage[0].seq;
      }
    }
    hasOlderConversationEvents = oldestPage.length === CONVERSATION_EVENTS_PAGE;
    conversationEventsInitialized = true;
    renderHistoryLoader();
    return;
  }
  for (;;) {
    const since = conversationEventsNewestSeq;
    const response = await api(
      "GET",
      `/apps/${encodeURIComponent(threadId)}/conversation/events?since=${since}`,
    );
    if (selectedAppId !== threadId || selectedRefreshSequence !== refreshSequence) return;
    const events = response.events || [];
    const fresh = events.filter(event => event.seq > since);
    if (fresh.length) {
      mergeConversationEvents(fresh);
      conversationEventsNewestSeq = fresh[fresh.length - 1].seq;
      if (conversationEventsOldestSeq === null) conversationEventsOldestSeq = fresh[0].seq;
    }
    if (fresh.length < CONVERSATION_EVENTS_PAGE) return;
  }
}

async function loadOlderConversationEvents() {
  if (
    !selectedAppId
    || !conversationEventsInitialized
    || !hasOlderConversationEvents
    || loadingOlderConversationEvents
    || conversationEventsOldestSeq === null
  ) return;
  const threadId = selectedAppId;
  const before = conversationEventsOldestSeq;
  loadingOlderConversationEvents = true;
  renderHistoryLoader();
  try {
    const response = await api(
      "GET",
      `/apps/${encodeURIComponent(threadId)}/conversation/events?before=${before}`,
    );
    if (selectedAppId !== threadId || conversationEventsOldestSeq !== before) return;
    const older = (response.events || []).filter(event => event.seq < before);
    const history = $("chat-history");
    const previousHeight = history.scrollHeight;
    const previousTop = history.scrollTop;
    if (older.length) {
      mergeConversationEvents(older);
      conversationEventsOldestSeq = older[0].seq;
    }
    hasOlderConversationEvents = older.length === CONVERSATION_EVENTS_PAGE;
    renderChat();
    history.scrollTop = previousTop + (history.scrollHeight - previousHeight);
    lastChatScrollTop = history.scrollTop;
  } finally {
    if (selectedAppId === threadId) {
      loadingOlderConversationEvents = false;
      renderHistoryLoader();
    }
  }
}

function renderHistoryLoader() {
  const loader = $("history-loader");
  loader.hidden = !selectedAppId || !hasOlderConversationEvents;
  loader.dataset.oldestSeq = conversationEventsOldestSeq === null
    ? ""
    : String(conversationEventsOldestSeq);
  const button = $("load-earlier");
  button.disabled = loadingOlderConversationEvents;
  button.textContent = loadingOlderConversationEvents
    ? "Loading earlier messages…"
    : "Load earlier messages";
}

function sessionConfigurationChanged() {
  return Boolean(establishedSession) && (
    $("runtime").value !== establishedSession.agent_runtime
    || $("model").value !== establishedSession.model
    || $("effort").value !== establishedSession.effort
  );
}

function setSessionOptions(preferredModel = null, preferredEffort = null) {
  const runtimeSelect = $("runtime");
  const modelSelect = $("model");
  const effortSelect = $("effort");
  const running = snapshot.status === "running";
  if (running && establishedSession) {
    setRuntimeOptions(establishedSession.agent_runtime);
    preferredModel = establishedSession.model;
    preferredEffort = establishedSession.effort;
  }
  const runtime = runtimeSelect.value;
  const models = sessionOptions[runtime] || {};
  const modelValues = Object.keys(models);
  const preservingRecordedSession = (
    establishedSession && runtime === establishedSession.agent_runtime
  );
  if (
    preservingRecordedSession
    && establishedSession.model
    && !modelValues.includes(establishedSession.model)
  ) {
    modelValues.push(establishedSession.model);
  }
  const currentModel = preferredModel || modelSelect.value;
  modelSelect.replaceChildren(...modelValues.map(value => new Option(value, value)));
  modelSelect.value = modelValues.includes(currentModel)
    ? currentModel
    : modelValues[0] || "";
  const efforts = [...(models[modelSelect.value] || [])];
  if (
    preservingRecordedSession
    && modelSelect.value === establishedSession.model
    && establishedSession.effort
    && !efforts.includes(establishedSession.effort)
  ) {
    efforts.push(establishedSession.effort);
  }
  const currentEffort = preferredEffort || effortSelect.value;
  effortSelect.replaceChildren(...efforts.map(value => new Option(value, value)));
  effortSelect.value = efforts.includes(currentEffort)
    ? currentEffort
    : efforts[0] || "";
  const activeSettingsLock = running || messageBusyApps.has(selectedAppId);
  const settingsLocked = activeSettingsLock || selectedAppArchived;
  runtimeSelect.disabled = settingsLocked;
  modelSelect.disabled = settingsLocked || !modelSelect.value;
  effortSelect.disabled = settingsLocked || !effortSelect.value;
  $("agent-settings").classList.toggle("active-locked", activeSettingsLock);
  if (!activeSettingsLock) {
    $("agent-settings").classList.remove("show-lock-note");
  }
  $("agent-session-change-warning").hidden = (
    settingsLocked
    || selectedAppArchived
    || !sessionConfigurationChanged()
  );
  const activeBlock = running && establishedSession?.agent_runtime === "hermes";
  const hasOversizedAttachment = pendingAttachments.some(
    attachment => attachment.size_bytes > ATTACHMENT_MAX_BYTES,
  );
  const attachmentBusy = attachmentActivities.has(selectedAppId);
  $("composer-running").hidden = !running;
  $("message").disabled = activeBlock;
  $("message").placeholder = running
    ? activeBlock
      ? "Hermes does not support follow-ups while running"
      : "Send another message"
    : "Describe the app or ask about its data";
  $("send-message").disabled = (
    !selectedAppId || messageBusyApps.has(selectedAppId) || selectedAppArchived
    || activeBlock
    || attachmentBusy
    || hasOversizedAttachment
    || !modelSelect.value
    || !effortSelect.value
  );
  const composerSending = messageBusyApps.has(selectedAppId);
  $("send-message").classList.toggle("sending", composerSending);
  $("send-message").setAttribute("aria-busy", String(composerSending));
  $("send-message").setAttribute(
    "aria-label",
    composerSending ? "Sending message" : "Send message",
  );
  $("send-message").title = composerSending ? "Sending message" : "Send";
  $("attach-file").disabled = (
    !selectedAppId
    || messageBusyApps.has(selectedAppId)
    || selectedAppArchived
    || activeBlock
    || attachmentBusy
    || pendingAttachments.length >= ATTACHMENT_LIMIT
  );
}

function setRuntimeOptions(preferredRuntime = null) {
  const labels = { codex: "Codex", claude_code: "Claude Code", hermes: "Hermes" };
  const current = preferredRuntime || $("runtime").value;
  const runtimes = Object.keys(sessionOptions);
  if (current && !runtimes.includes(current)) runtimes.push(current);
  $("runtime").replaceChildren(...runtimes.map(
    value => new Option(labels[value] || value, value)
  ));
  if (runtimes.includes(current)) $("runtime").value = current;
}

function syncAgentSettings(task) {
  const next = task ? {
    agent_runtime: String(task.agent_runtime || ""),
    model: String(task.model || ""),
    effort: String(task.effort || ""),
  } : null;
  const key = next ? `${next.agent_runtime}\0${next.model}\0${next.effort}` : "open";
  if (key === establishedSessionKey) return;
  establishedSession = next;
  establishedSessionKey = key;
  setRuntimeOptions(next?.agent_runtime || null);
  setSessionOptions(next?.model || null, next?.effort || null);
}

function runtimeLabel(value) {
  return { codex: "Codex", claude_code: "Claude Code", hermes: "Hermes" }[value] || value || "No session";
}

function relativeTime(value) {
  const timestamp = Date.parse(value || "");
  if (!Number.isFinite(timestamp)) return "just now";
  const seconds = Math.max(0, Math.floor((Date.now() - timestamp) / 1000));
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m ago`;
  if (seconds < 86400) return `${Math.floor(seconds / 3600)}h ago`;
  if (seconds < 604800) return `${Math.floor(seconds / 86400)}d ago`;
  return new Date(timestamp).toLocaleDateString();
}

function renderApps() {
  const key = JSON.stringify([selectedAppId, showingArchivedApps, apps]);
  if (key === renderedAppsKey) return;
  renderedAppsKey = key;
  const list = $("apps");
  list.replaceChildren();
  if (!apps.length) {
    const empty = document.createElement("div");
    empty.className = "sidebar-empty";
    empty.textContent = showingArchivedApps ? "No archived apps." : "No apps yet.";
    list.append(empty);
    return;
  }
  for (const app of apps) {
    const item = document.createElement("button");
    item.className = `app-item${app.thread_id === selectedAppId ? " selected" : ""}`;
    item.dataset.appId = app.thread_id;

    const name = document.createElement("span");
    name.className = "app-name";
    const label = document.createElement("span");
    label.textContent = app.name;
    name.append(label);
    if (app.status === "running") {
      const dot = document.createElement("span");
      dot.className = "app-dot";
      name.append(dot);
    }

    const session = document.createElement("span");
    session.className = "app-meta";
    session.textContent = app.session
      ? `${runtimeLabel(app.session.agent_runtime)} · ${app.session.model}`
      : "No agent session";

    const details = document.createElement("span");
    details.className = "app-meta";
    details.textContent = `Revision ${app.revision} · ${relativeTime(app.last_used_at)}`;
    item.append(name, session, details);
    list.append(item);
  }
}

function syncWorkspaceControls() {
  const hasApp = selectedAppId !== null;
  $("app-title").textContent = hasApp
    ? selectedAppName || selectedAppId
    : showingArchivedApps ? "Archived apps" : "Select an app";
  $("rename-app").hidden = !hasApp;
  $("archive-app").hidden = !hasApp;
  $("archive-app").textContent = selectedAppArchived ? "Unarchive" : "Archive";
  $("open-chat").hidden = !hasApp;
  $("agent-settings").hidden = !hasApp;
  $("chat-composer").hidden = !hasApp || selectedAppArchived;
  $("chat-subtitle").textContent = selectedAppArchived
    ? "Read-only while this app is archived"
    : "Build, edit, or ask about this app";
  $("generated-host").classList.toggle("readonly", selectedAppArchived);
  $("new-app").hidden = showingArchivedApps;
  $("archived-toggle").textContent = showingArchivedApps ? "Show active" : "Show archived";
  if (!hasApp) $("revision-label").textContent = "";
  setSessionOptions();
  syncEmptyState();
}

function stopCapabilityWorker() {
  if (workerRun) workerRun.finish("workspace-switched");
}

function saveSelectedConversationView() {
  if (!selectedAppId) return;
  // Refresh insertion order so the oldest unvisited app is evicted first.
  conversationViewStates.delete(selectedAppId);
  conversationViewStates.set(selectedAppId, {
    session: snapshot.session,
    status: snapshot.status,
    events: conversationEvents,
    oldestSeq: conversationEventsOldestSeq,
    newestSeq: conversationEventsNewestSeq,
    initialized: conversationEventsInitialized,
    hasOlder: hasOlderConversationEvents,
    scrollTop: $("chat-history").scrollTop,
  });
  if (conversationViewStates.size > VIEW_STATE_LIMIT) {
    conversationViewStates.delete(conversationViewStates.keys().next().value);
  }
}

function restoreConversationView(app) {
  const state = conversationViewStates.get(app.thread_id);
  if (!state) {
    snapshot = { app: null, session: app.session || null, status: app.status || "idle" };
    conversationEvents = [];
    conversationEventsOldestSeq = null;
    conversationEventsNewestSeq = 0;
    conversationEventsInitialized = false;
    hasOlderConversationEvents = false;
    lastChatScrollTop = 0;
    restoredChatScrollTop = null;
  } else {
    snapshot = {
      app: null,
      session: app.session || state.session || null,
      status: app.status || state.status || "idle",
    };
    conversationEvents = state.events;
    conversationEventsOldestSeq = state.oldestSeq;
    conversationEventsNewestSeq = state.newestSeq;
    conversationEventsInitialized = state.initialized;
    hasOlderConversationEvents = state.hasOlder;
    lastChatScrollTop = state.scrollTop;
    restoredChatScrollTop = state.scrollTop;
  }
  loadingOlderConversationEvents = false;
  renderHistoryLoader();
}

function clearSelectedApp() {
  saveComposerDraft();
  saveSelectedConversationView();
  clearPendingAttachments();
  stopCapabilityWorker();
  selectedRefreshSequence += 1;
  selectedAppId = null;
  selectedAppName = null;
  selectedAppArchived = false;
  restoreComposerDraft();
  snapshot = { app: null, session: null, status: "idle" };
  conversationEvents = [];
  conversationEventsOldestSeq = null;
  conversationEventsNewestSeq = 0;
  conversationEventsInitialized = false;
  hasOlderConversationEvents = false;
  loadingOlderConversationEvents = false;
  lastChatScrollTop = 0;
  restoredChatScrollTop = null;
  renderedChatEntries.clear();
  renderedRevision = -1;
  establishedSession = null;
  establishedSessionKey = "";
  clearGenerated();
  closeChat();
  renderChat();
  renderApps();
  syncWorkspaceControls();
}

async function showApp(app) {
  saveComposerDraft();
  saveSelectedConversationView();
  if (selectedAppId !== app.thread_id) clearPendingAttachments();
  stopCapabilityWorker();
  selectedRefreshSequence += 1;
  selectedAppId = app.thread_id;
  selectedAppName = app.name;
  selectedAppArchived = Boolean(app.archived);
  restoreComposerDraft();
  restoreConversationView(app);
  renderedChatEntries.clear();
  renderedRevision = -1;
  establishedSession = null;
  establishedSessionKey = "";
  clearGenerated();
  closeChat();
  renderChat();
  renderApps();
  syncWorkspaceControls();
  setSidebarOpen(false);
  await refreshSelectedApp(app.thread_id);
}

async function refreshSelectedApp(threadId = selectedAppId) {
  if (!threadId || threadId !== selectedAppId) return;
  const refreshSequence = ++selectedRefreshSequence;
  const [stateResponse, conversationResponse] = await Promise.all([
    api("GET", `/apps/${encodeURIComponent(threadId)}/state`),
    api("GET", `/apps/${encodeURIComponent(threadId)}/conversation`),
  ]);
  if (threadId !== selectedAppId || selectedRefreshSequence !== refreshSequence) return;
  const listedSession = apps.find(app => app.thread_id === threadId)?.session || null;
  const next = {
    app: stateResponse.app,
    // A fixed host session outlives retained history. The app index reads it
    // from the host thread summary, so an empty conversation response must
    // not make an established workspace look configurable again.
    session: conversationResponse.session || listedSession || snapshot.session || null,
    status: conversationResponse.status || "idle",
  };
  await refreshConversationEvents(threadId, refreshSequence);
  if (threadId !== selectedAppId || selectedRefreshSequence !== refreshSequence) return;
  if (snapshot.app && next.app.revision < snapshot.app.revision) next.app = snapshot.app;
  snapshot = next;
  if (next.app.revision !== renderedRevision) {
    stopCapabilityWorker();
    renderedRevision = next.app.revision;
    if (next.app.html || next.app.css || next.app.javascript) {
      $("revision-label").textContent = `Revision ${next.app.revision}`;
      renderGenerated(next.app.html, next.app.css);
      if (!selectedAppArchived) runCapabilityWorker();
    } else {
      $("revision-label").textContent = "Empty app";
      clearGenerated();
    }
  }
  renderChat();
  syncAgentSettings(snapshot.session);
  syncWorkspaceControls();
}

async function refresh() {
  const refreshSequence = ++appsRefreshSequence;
  const archivedView = showingArchivedApps;
  try {
    const response = await api("GET", archivedView ? "/apps?archived=true" : "/apps");
    if (
      refreshSequence !== appsRefreshSequence
      || archivedView !== showingArchivedApps
    ) return;
    apps = response.apps || [];
    const selected = apps.find(app => app.thread_id === selectedAppId);
    if (selected) {
      selectedAppName = selected.name;
      selectedAppArchived = Boolean(selected.archived);
    } else if (selectedAppId) {
      clearSelectedApp();
    }
    renderApps();
    syncWorkspaceControls();
    if (selectedAppId) await refreshSelectedApp(selectedAppId);
  } catch (_error) {
    if (refreshSequence === appsRefreshSequence) {
      showRuntimeStatus("Agentic Web App backend unavailable", "error");
    }
  }
}

async function createApp() {
  const button = $("new-app");
  button.disabled = true;
  try {
    const response = await api("POST", "/apps", {});
    showingArchivedApps = false;
    await refresh();
    const app = apps.find(candidate => candidate.thread_id === response.app.thread_id) || response.app;
    await showApp(app);
  } catch (error) {
    showRuntimeStatus(error.message || "Could not create app", "error");
  } finally {
    button.disabled = false;
  }
}

async function renameSelectedApp() {
  if (!selectedAppId) return;
  const threadId = selectedAppId;
  const requestedName = prompt("Rename app (max 100 characters):", selectedAppName || threadId);
  if (requestedName === null) return;
  const name = requestedName.trim();
  if (!name) {
    showRuntimeStatus("App name cannot be empty", "error");
    return;
  }
  const response = await api(
    "PUT",
    `/apps/${encodeURIComponent(threadId)}/name`,
    { name },
  );
  apps = apps.map(app => app.thread_id === threadId ? { ...app, name: response.app.name } : app);
  if (selectedAppId === threadId) selectedAppName = response.app.name;
  renderedAppsKey = "";
  renderApps();
  syncWorkspaceControls();
}

async function setSelectedAppArchived() {
  if (!selectedAppId) return;
  const threadId = selectedAppId;
  const action = selectedAppArchived ? "unarchive" : "archive";
  await api("POST", `/apps/${encodeURIComponent(threadId)}/${action}`, {});
  if (selectedAppId === threadId) clearSelectedApp();
  await refresh();
}

async function toggleArchivedApps() {
  showingArchivedApps = !showingArchivedApps;
  clearSelectedApp();
  await refresh();
}

// Must match the drawer breakpoint in the stylesheet.
const drawerMedia = window.matchMedia("(max-width: 720px)");

function setSidebarOpen(open, restoreFocus = false) {
  const mobile = drawerMedia.matches;
  const isOpen = mobile && open;
  const pane = document.querySelector(".app-pane");
  $("builder-shell").classList.toggle("sidebar-open", isOpen);
  pane.inert = mobile && !isOpen;
  document.querySelector(".workspace-main").inert = isOpen;
  $("sidebar-backdrop").hidden = !isOpen;
  $("sidebar-open").setAttribute("aria-expanded", String(isOpen));
  if (isOpen) $("sidebar-close").focus();
  else if (restoreFocus && mobile) $("sidebar-open").focus();
}

async function initialize() {
  generatedRoot = $("generated-host").attachShadow({ mode: "open" });
  generatedRoot.addEventListener("click", generatedInteraction);
  generatedRoot.addEventListener("change", generatedInteraction);
  generatedRoot.addEventListener("submit", event => event.preventDefault());
  try {
    const options = await api("GET", "/session-options");
    sessionOptions = options.session_options || {};
    setRuntimeOptions();
    setSessionOptions();
  } catch (_error) {
    showRuntimeStatus("Agent settings are unavailable", "error");
  }
  setSidebarOpen(false);
  clearSelectedApp();
  await refresh();
  setInterval(refresh, 3000);
}

document.addEventListener("click", event => {
  const linkButton = event.target.closest && event.target.closest(".md-copy-link");
  if (linkButton) {
    requestHostCopy(linkButton.dataset.copyHref || "").then(() => {
      linkButton.textContent = "Copied";
      setTimeout(() => { linkButton.textContent = linkButton.dataset.copyHref || ""; }, 1200);
    }).catch(error => showChatStatus(error.message, true));
    return;
  }
  const copyButton = event.target.closest && event.target.closest(".md-copy");
  if (copyButton) {
    const code = copyButton.closest(".md-code")?.querySelector("code")?.textContent || "";
    requestHostCopy(code).then(() => {
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy"; }, 1200);
    }).catch(error => showChatStatus(error.message, true));
    return;
  }
  const removeAttachmentButton = event.target.closest
    && event.target.closest("button[data-remove-attachment]");
  if (removeAttachmentButton) {
    removeAttachment(removeAttachmentButton.dataset.removeAttachment)
      .catch(error => showChatStatus(error.message, true));
    return;
  }
  const item = event.target.closest && event.target.closest(".app-item");
  if (!item) return;
  const app = apps.find(candidate => candidate.thread_id === item.dataset.appId);
  if (app) void showApp(app).catch(error => showRuntimeStatus(error.message, "error"));
});
$("new-app").addEventListener("click", () => createApp());
$("archived-toggle").addEventListener("click", () => toggleArchivedApps());
$("rename-app").addEventListener("click", () => renameSelectedApp().catch(error => showRuntimeStatus(error.message, "error")));
$("archive-app").addEventListener("click", () => setSelectedAppArchived().catch(error => showRuntimeStatus(error.message, "error")));
$("open-chat").addEventListener("click", () => $("chat-drawer").hidden ? openChat() : closeChat());
$("empty-primary").addEventListener("click", () => {
  if (!selectedAppId) {
    if (showingArchivedApps) void toggleArchivedApps();
    else void createApp();
  } else if (selectedAppArchived) {
    void setSelectedAppArchived().catch(error => showRuntimeStatus(error.message, "error"));
  } else {
    openChat();
  }
});
$("close-chat").addEventListener("click", closeChat);
$("sidebar-open").addEventListener("click", () => setSidebarOpen(true));
$("sidebar-close").addEventListener("click", () => setSidebarOpen(false, true));
$("sidebar-backdrop").addEventListener("click", () => setSidebarOpen(false, true));
drawerMedia.addEventListener("change", () => setSidebarOpen(false));
$("runtime").addEventListener("change", () => setSessionOptions());
$("model").addEventListener("change", () => setSessionOptions($("model").value));
$("effort").addEventListener("change", () => setSessionOptions());
$("agent-settings").addEventListener("mouseenter", () => {
  if ($("agent-settings").classList.contains("active-locked")) {
    $("agent-settings").classList.add("show-lock-note");
  }
});
$("agent-settings").addEventListener("mouseleave", () => {
  $("agent-settings").classList.remove("show-lock-note");
});
$("load-earlier").addEventListener("click", () => {
  loadOlderConversationEvents().catch(error => showChatStatus(error.message, true));
});
$("chat-history").addEventListener("scroll", () => {
  const history = $("chat-history");
  const movedUp = history.scrollTop < lastChatScrollTop;
  lastChatScrollTop = history.scrollTop;
  if (!movedUp || history.scrollTop > 160) return;
  loadOlderConversationEvents().catch(error => showChatStatus(error.message, true));
}, { passive: true });
$("send-message").addEventListener("click", () => sendMessage());
$("attach-file").addEventListener("click", () => {
  attachFile().catch(error => showChatStatus(error.message, true));
});
$("stop-turn").addEventListener("click", () => {
  stopRunningTurn().catch(error => showChatStatus(error.message, true));
});
$("message").addEventListener("keydown", event => {
  if (event.key === "Enter" && !event.shiftKey) {
    event.preventDefault();
    sendMessage();
  }
});
$("message").addEventListener("input", saveComposerDraft);
initialize();
