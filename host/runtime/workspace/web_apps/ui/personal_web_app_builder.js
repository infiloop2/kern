(() => {
"use strict";

const MAX_WORKER_MESSAGES_PER_SECOND = 100;
const MAX_WORKER_MESSAGES_PER_TURN = 128;
const MAX_WORKER_MUTATIONS_PER_TURN = 16;
const WORKER_START_TIMEOUT_MS = 15 * 1000;
const WORKER_TURN_TIMEOUT_MS = 3000;
const MAX_RENDER_HTML_BYTES = 128 * 1024;
const MAX_RENDER_CSS_BYTES = 64 * 1024;
const MAX_RENDER_NODES = 5000;
const MAX_RENDER_DEPTH = 128;
const MAX_CSS_RULES = 4096;
const MAX_CSS_RULE_DEPTH = 16;
const MAX_CSS_CONDITION_BYTES = 512;
const MAX_AGENT_MESSAGE_BYTES = 4000;
const MAX_EVENT_FIELDS = 64;
const MAX_EVENT_FIELD_BYTES = 8192;
const MAX_EVENT_PAYLOAD_BYTES = 64 * 1024;
const MAX_DATA_VALUE_BYTES = 256 * 1024;
const CONVERSATION_EVENTS_PAGE = 6;
const INITIAL_CONVERSATION_EVENT_PAGES = 1;
const VIEW_STATE_LIMIT = 50;
const ATTACHMENT_LIMIT = 10;
const ATTACHMENT_MAX_BYTES = 25 * 1024 * 1024;
const WORKSPACE_API_TIMEOUT_MS = 12000;
// The browser Workspace proxy can wait 50s for a synchronous provider
// acknowledgement. Sends and Stop must outlive that hop or a retry can
// duplicate a message the host accepted after the frame gave up.
const AGENT_DELIVERY_TIMEOUT_MS = 60 * 1000;
const COMPOSER_DRAFTS_STORAGE_KEY = "kern.agentic-web-app.composer-drafts.v1";
const COMPOSER_DRAFT_LIMIT = 50;
const DISMISSED_AGENT_MESSAGES_STORAGE_KEY = "kern.agentic-web-app.dismissed-agent-messages.v1";
const DISMISSED_AGENT_MESSAGE_LIMIT = 50;
const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();
let requestCounter = 0;
const localFiles = new Map();
let sessionOptions = {};
let activeRuntimes = null;
let apps = [];
let selectedAppId = null;
let selectedAppName = null;
let selectedAgentUpdatesLocked = false;
let agentUpdateLockBusy = false;
let selectedAppOutsideActiveIndex = false;
let snapshot = { app: null, session: null, status: "idle" };
let conversationEvents = [];
let conversationEventPages = freshConversationEventPages();
const conversationViewStates = new Map();
let renderedRevision = -1;
let pendingApp = null;
let generatedRoot = null;
let generatedStyleSheet = null;
let generatedStyleLink = null;
let generatedStyleUrl = null;
let workerRun = null;
let armedWorker = null;
let bundleUrl = null;
let lastRenderKey = "";
let cssCacheKey = null;
let cssCacheValue = "";
let generatedDrag = null;
let appsRefreshSequence = 0;
let appSelectionSequence = 0;
let createAppPromise = null;
const messageBusyApps = new Set();
const sessionAgentMessageApps = new Set();
let selectedRefreshSequence = 0;
let establishedSession = null;
let establishedSessionKey = "";
let pendingAttachments = [];
const attachmentActivities = new Map();
let panelRefreshSequence = 0;
let recoveryPoints = [];
let runtimeStatusSequence = 0;
let dismissedAgentMessageKey = null;
let transientAgentStatus = null;
let renameAppReturnFocus = null;
let historyMode = false;
let historyLoadingOlder = false;
let historyRenderedAppId = null;
let historyRenderedEntryKey = "";

const webAppsRoot = window.KernWorkspaceRoots["web-apps"];
const $ = id => webAppsRoot.querySelector(`#${CSS.escape(id)}`);
const composerDrafts = loadComposerDrafts();
const dismissedAgentMessages = loadDismissedAgentMessages();

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

function clearComposerDraft(appId, submittedDraft) {
  const key = `app:${appId}`;
  if ((composerDrafts[key] ?? "") !== submittedDraft) return false;
  delete composerDrafts[key];
  persistComposerDrafts();
  return true;
}

function loadDismissedAgentMessages() {
  try {
    const stored = JSON.parse(
      localStorage.getItem(DISMISSED_AGENT_MESSAGES_STORAGE_KEY) || "{}"
    );
    if (!stored || typeof stored !== "object" || Array.isArray(stored)) return {};
    return Object.fromEntries(
      Object.entries(stored).filter(([, key]) => typeof key === "string")
    );
  } catch (_error) {
    return {};
  }
}

function persistDismissedAgentMessages() {
  try {
    localStorage.setItem(
      DISMISSED_AGENT_MESSAGES_STORAGE_KEY,
      JSON.stringify(dismissedAgentMessages),
    );
  } catch (_error) {
    // Dismissal persistence is best-effort when browser storage is unavailable.
  }
}

function setDismissedAgentMessage(appId, key) {
  if (!appId) return;
  delete dismissedAgentMessages[appId];
  if (key) dismissedAgentMessages[appId] = key;
  while (Object.keys(dismissedAgentMessages).length > DISMISSED_AGENT_MESSAGE_LIMIT) {
    delete dismissedAgentMessages[Object.keys(dismissedAgentMessages)[0]];
  }
  persistDismissedAgentMessages();
  if (selectedAppId === appId) dismissedAgentMessageKey = key || null;
}

function api(method, path, body, timeoutMs = WORKSPACE_API_TIMEOUT_MS) {
  return Promise.race([
    window.KernHost.api(method, `/v1/workspace/web-apps${path}`, body),
    new Promise((_, reject) => setTimeout(() => reject(new Error("App request timed out")), timeoutMs)),
  ]);
}

async function requestHostCopy(text) {
  await navigator.clipboard.writeText(text);
  return { copied: true };
}

async function requestFileUpload(action, selectionId, maximumFiles) {
  if (action === "select") {
    const files = await window.KernHost.chooseFiles(maximumFiles || ATTACHMENT_LIMIT);
    if (files === null) return null;
    return { selections: files.map(file => {
      const selection_id = `web-${++requestCounter}`;
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

function capabilityWorkerBootstrap(maxRenderHtmlBytes, maxRenderCssBytes) {
  "use strict";
  // The trusted broker's CSP is authoritative. Function.prototype.constructor can still
  // recover the real Function constructor and WebAssembly remains available,
  // so script-src must keep unsafe-eval and wasm-unsafe-eval absent. The
  // generated data worker inherits that policy and connect-src 'none'.
  // Scrubbing common globals below is defense in depth, not the code-execution
  // or egress bound.
  // Timers and message channels are denied so a pre-armed worker waiting for
  // its event turn has no way to schedule background work: with an empty event
  // loop, only trusted frame messages can wake generated code.
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
    "setTimeout", "setInterval", "clearTimeout", "clearInterval", "setImmediate",
    "MessageChannel", "MessagePort",
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
  let targetedData = false;
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
    pending.set(id, { resolve, reject, action, path: clone(path) });
    const message = { type: "data-action", request_id: id, action, path: clone(path) };
    if (includeValue) message.value = clone(value);
    send(message);
  });
  const read = path => new Promise((resolve, reject) => {
    const id = `read-${++requestId}`;
    pending.set(id, { resolve, reject });
    send({ type: "data-read", request_id: id, path: clone(path) });
  });
  const query = (collection, request = {}) => new Promise((resolve, reject) => {
    const id = `query-${++requestId}`;
    pending.set(id, { resolve, reject });
    send({
      type: "collection-query",
      request_id: id,
      collection,
      query: clone(request),
    });
  });
  const applyMutation = (root, action, path, value) => {
    const updated = clone(root);
    let parent = updated;
    for (const segment of path.slice(0, -1)) parent = parent[segment];
    const leaf = path[path.length - 1];
    if (action === "append") parent[leaf].push(clone(value));
    else if (action === "delete") {
      if (Array.isArray(parent)) parent.splice(leaf, 1);
      else delete parent[leaf];
    } else if (Array.isArray(parent)) parent[leaf] = clone(value);
    else Object.defineProperty(parent, leaf, {
      value: clone(value), writable: true, enumerable: true, configurable: true,
    });
    return updated;
  };
  const api = Object.freeze({
    onLoad(handler, options = {}) {
      if (typeof handler !== "function") throw new TypeError("handler must be a function");
      if (!options || typeof options !== "object" || ![undefined, "targeted"].includes(options.data)) {
        throw new TypeError("onLoad data mode must be targeted when provided");
      }
      targetedData = options.data === "targeted";
      loadHandler = handler;
    },
    on(action, handler) {
      action = actionName(action);
      if (typeof handler !== "function") throw new TypeError("handler must be a function");
      handlers.set(action, handler);
    },
    data() {
      if (targetedData) throw new Error("app.data() is unavailable in targeted data mode; use app.read(path)");
      return clone(durableData);
    },
    read(path) { return read(path); },
    query(collection, request) { return query(collection, request); },
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
        .catch(() => send({ type: "initialization-error", reason: "load-handler" }));
      return;
    }
    if (message.type === "data-result") {
      const waiter = pending.get(message.request_id);
      if (!waiter) return;
      pending.delete(message.request_id);
      if (message.ok) {
        if (targetedData) {
          waiter.resolve(waiter.action === "delete" ? null : clone(message.value));
        } else {
          durableData = applyMutation(
            durableData, waiter.action, waiter.path, message.value,
          );
          waiter.resolve(clone(durableData));
        }
      } else {
        waiter.reject(new Error("Data update failed"));
      }
      return;
    }
    if (message.type === "read-result") {
      const waiter = pending.get(message.request_id);
      if (!waiter) return;
      pending.delete(message.request_id);
      if (message.ok) waiter.resolve(clone(message.value));
      else waiter.reject(new Error("Data read failed"));
      return;
    }
    if (message.type === "collection-query-result") {
      const waiter = pending.get(message.request_id);
      if (!waiter) return;
      pending.delete(message.request_id);
      if (message.ok) waiter.resolve(clone(message.collection));
      else waiter.reject(new Error("Collection query failed"));
      return;
    }
    if (message.type === "event") {
      const handler = handlers.get(message.action);
      resolvePromise(handler ? handler(clone(message.event)) : undefined)
        .then(() => send({ type: "turn-complete", turn_id: message.turn_id }))
        .catch(() => send({
          type: "turn-error", turn_id: message.turn_id, reason: "action-handler",
        }));
    }
  });
  // The generated source is appended after this bootstrap invocation. Defer
  // readiness one microtask so its top-level onLoad registration can select
  // targeted data before the trusted frame decides whether to fetch data.
  resolvePromise().then(() => send({
    type: "ready", data_mode: targetedData ? "targeted" : "full",
  }));
}

const allowedElements = new Set([
  "A", "ABBR", "ADDRESS", "ARTICLE", "ASIDE", "BDI", "BDO", "BLOCKQUOTE", "BR",
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
  "AUDIO", "BASE", "EMBED", "IFRAME", "IMG", "LINK", "META", "OBJECT",
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
// iOS zooms and pans the visual viewport when a focused text control computes
// below 16px. Generated Apps may choose smaller typography, but their editable
// controls must remain stable while the software keyboard owns the viewport.
const generatedMobileTextControlCss = `@media(max-width:720px){input:not([type]),input[type="text"],input[type="search"],input[type="email"],input[type="tel"],input[type="url"],input[type="password"],input[type="number"],textarea,select{font-size:max(16px,1em)!important}}`;
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
  const budget = { nodes: 0 };
  for (const node of template.content.childNodes) cloneSafeNode(node, output, budget, 0);
  return output;
}

function cloneSafeNode(node, parent, budget, depth) {
  if (++budget.nodes > MAX_RENDER_NODES || depth > MAX_RENDER_DEPTH) {
    throw new Error("Generated HTML is too complex");
  }
  if (node.nodeType === Node.TEXT_NODE) {
    parent.append(document.createTextNode(node.data));
    return;
  }
  if (node.nodeType !== Node.ELEMENT_NODE) return;
  if (droppedElements.has(node.tagName)) return;
  const rawHref = node.tagName === "A" ? node.getAttribute("href") : "";
  const navigationHref = node.tagName === "A" ? KernRichText.safeNavigationHref(rawHref) : "";
  const copyHref = node.tagName === "A" && !navigationHref ? KernRichText.safeHref(rawHref) : "";
  if (node.tagName === "A" && !navigationHref && !copyHref) {
    for (const child of node.childNodes) cloneSafeNode(child, parent, budget, depth + 1);
    return;
  }
  if (!allowedElements.has(node.tagName)) {
    for (const child of node.childNodes) cloneSafeNode(child, parent, budget, depth + 1);
    return;
  }
  const clean = document.createElement(node.tagName === "A" && copyHref ? "button" : node.tagName.toLowerCase());
  for (const attribute of node.attributes) copySafeAttribute(node, clean, attribute.name, attribute.value);
  if (node.tagName === "A") {
    clean.removeAttribute("data-action");
    clean.removeAttribute("data-enter-action");
    clean.removeAttribute("data-drop-action");
    if (navigationHref) {
      clean.setAttribute("href", navigationHref);
      clean.setAttribute("title", navigationHref);
      clean.setAttribute("target", "_blank");
      clean.setAttribute("rel", "noopener noreferrer");
    } else {
      clean.type = "button";
      clean.classList.add("kern-copy-link");
      clean.setAttribute("data-kern-copy-href", copyHref);
      clean.setAttribute("title", "Copy link");
    }
  }
  if (clean.hasAttribute("data-drag-value")) clean.draggable = true;
  if (node.tagName === "BUTTON") clean.type = "button";
  if (node.tagName === "INPUT") {
    const type = node.getAttribute("type") || "text";
    clean.type = allowedInputTypes.has(type.toLowerCase()) ? type.toLowerCase() : "text";
  }
  for (const child of node.childNodes) cloneSafeNode(child, clean, budget, depth + 1);
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
  if (lower === "data-enter-action" && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (lower === "data-drop-action" && /^[A-Za-z][A-Za-z0-9_-]{0,63}$/.test(value)) {
    target.setAttribute(lower, value);
    return;
  }
  if (lower === "data-drag-value" || lower === "data-drop-value") {
    target.setAttribute(lower, clipEncodedText(value, MAX_EVENT_FIELD_BYTES));
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
  const budget = { rules: 0 };
  return Array.from(sheet.cssRules, rule => sanitizeRule(rule, budget, 0)).filter(Boolean).join("\n");
}

function sanitizeCssCached(css) {
  // Re-renders usually change markup, not styles; skip the CSSOM round trip
  // when the stylesheet text is unchanged.
  if (css === cssCacheKey) return cssCacheValue;
  const value = sanitizeCss(css);
  cssCacheKey = css;
  cssCacheValue = value;
  return value;
}

function sanitizeRule(rule, budget, depth) {
  if (++budget.rules > MAX_CSS_RULES || depth > MAX_CSS_RULE_DEPTH) {
    throw new Error("Generated CSS is too complex");
  }
  const kind = rule.constructor.name;
  if (kind === "CSSStyleRule") {
    // Shadow CSS can otherwise restyle its host and escape the generated
    // canvas. Reject escapes too, so an encoded :host cannot bypass the check.
    if (rule.selectorText.includes("\\") || /:host(?:-context)?(?:\b|\()/i.test(rule.selectorText)) return "";
    return `${rule.selectorText}{${sanitizeDeclarations(rule.style)}}`;
  }
  if (kind === "CSSMediaRule") {
    if (textEncoder.encode(rule.conditionText).length > MAX_CSS_CONDITION_BYTES) return "";
    return `@media ${rule.conditionText}{${Array.from(
      rule.cssRules, child => sanitizeRule(child, budget, depth + 1)
    ).filter(Boolean).join("")}}`;
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

// --- Incremental rendering ---------------------------------------------------
// The sanitizers produce a fresh safe tree; instead of replacing the whole
// shadow root (which drops focus, selection, and scroll on every render), the
// existing tree is patched in place. Both trees came from the same sanitizer,
// so patching only ever copies already-sanitized nodes and attributes.

function renderGenerated(html, css) {
  const host = $("generated-host");
  if (!generatedRoot) generatedRoot = host.attachShadow({ mode: "open" });
  const renderKey = `${html}\u0000${css}`;
  if (renderKey !== lastRenderKey || !generatedRoot.firstChild) {
    clearGeneratedDrag();
    const fragment = sanitizeHtml(html);
    const safeCss = sanitizeCssCached(css);
    const styleText = `:host{display:block;min-height:100%;color:var(--text);background:var(--bg);font-family:system-ui,sans-serif}.kern-copy-link{background:transparent;border:0;color:inherit;cursor:pointer;font:inherit;padding:0;text-decoration:underline;text-underline-offset:.15em}${safeCss}${generatedMobileTextControlCss}`;
    patchChildren(generatedRoot, fragment);
    // Commit safe content before installing styles. A browser-specific style
    // failure must never strand the operator on the stored Loading placeholder.
    host.hidden = false;
    $("canvas-empty").hidden = true;
    installGeneratedStyles(styleText);
    lastRenderKey = renderKey;
  }
  host.hidden = false;
  $("canvas-empty").hidden = true;
}

function installGeneratedStyles(styleText) {
  if (generatedStyleSheet !== false) {
    try {
      if (!generatedStyleSheet) {
        generatedStyleSheet = new CSSStyleSheet();
        generatedRoot.adoptedStyleSheets = [generatedStyleSheet];
      }
      generatedStyleSheet.replaceSync(styleText);
      return;
    } catch (_error) {
      // Some otherwise supported browsers expose CSSStyleSheet construction
      // but reject adoptedStyleSheets on ShadowRoot. Fall back to a sanitized
      // blob stylesheet instead of failing before the App's HTML can render.
      generatedStyleSheet = false;
      try { generatedRoot.adoptedStyleSheets = []; }
      catch (_ignored) { /* The fallback does not depend on this assignment. */ }
    }
  }
  if (!generatedStyleLink) {
    generatedStyleLink = document.createElement("link");
    generatedStyleLink.rel = "stylesheet";
    generatedRoot.append(generatedStyleLink);
  }
  const previousUrl = generatedStyleUrl;
  const nextUrl = URL.createObjectURL(new Blob([styleText], { type: "text/css" }));
  generatedStyleUrl = nextUrl;
  generatedStyleLink.addEventListener("load", () => {
    if (previousUrl) URL.revokeObjectURL(previousUrl);
  }, { once: true });
  generatedStyleLink.href = nextUrl;
}

function patchChildren(parent, fragment) {
  const desired = Array.from(fragment.childNodes);
  let current = parent.firstChild;
  if (current === generatedStyleLink) current = null;
  for (const want of desired) {
    if (current === generatedStyleLink) current = null;
    if (!current) {
      parent.insertBefore(want, generatedStyleLink);
      continue;
    }
    current = patchNode(current, want).nextSibling;
  }
  while (current && current !== generatedStyleLink) {
    const next = current.nextSibling;
    current.remove();
    current = next;
  }
}

function patchNode(current, want) {
  if (current.nodeType === Node.TEXT_NODE && want.nodeType === Node.TEXT_NODE) {
    if (current.data !== want.data) current.data = want.data;
    return current;
  }
  if (
    current.nodeType !== want.nodeType
    || current.nodeType !== Node.ELEMENT_NODE
    || current.tagName !== want.tagName
  ) {
    current.replaceWith(want);
    return want;
  }
  if (current.isEqualNode(want)) {
    syncControlState(current, want);
    return current;
  }
  syncAttributes(current, want);
  const desired = Array.from(want.childNodes);
  let child = current.firstChild;
  for (const wantChild of desired) {
    if (!child) {
      current.appendChild(wantChild);
      continue;
    }
    child = patchNode(child, wantChild).nextSibling;
  }
  while (child) {
    const next = child.nextSibling;
    child.remove();
    child = next;
  }
  syncControlState(current, want);
  return current;
}

function syncAttributes(current, want) {
  for (const attribute of Array.from(current.attributes)) {
    if (!want.hasAttribute(attribute.name)) current.removeAttribute(attribute.name);
  }
  for (const attribute of want.attributes) {
    if (current.getAttribute(attribute.name) !== attribute.value) {
      current.setAttribute(attribute.name, attribute.value);
    }
  }
}

function syncControlState(current, want) {
  // Attribute sync only changes control defaults; a render that sets a new
  // value must also win over live user state — except in the control the user
  // is currently editing.
  if (!generatedRoot || generatedRoot.activeElement === current) return;
  const tag = current.tagName;
  if (tag === "INPUT") {
    if (current.type === "checkbox" || current.type === "radio") {
      if (current.checked !== want.checked) current.checked = want.checked;
    } else if (current.value !== want.value) {
      current.value = want.value;
    }
  } else if (tag === "TEXTAREA" || tag === "SELECT") {
    if (current.value !== want.value) current.value = want.value;
  }
}

function clearGenerated() {
  clearGeneratedDrag();
  if (generatedRoot) generatedRoot.replaceChildren();
  if (generatedStyleUrl) URL.revokeObjectURL(generatedStyleUrl);
  generatedStyleLink = null;
  generatedStyleUrl = null;
  lastRenderKey = "";
  $("generated-host").hidden = true;
  $("canvas-empty").hidden = false;
}

function syncCanvasState() {
  if (!selectedAppId) {
    $("empty-title").textContent = "Choose an app";
    $("empty-description").textContent = "Select an app or create a new one from the sidebar.";
    return;
  }
  const firstRun = !snapshot.session;
  $("empty-title").textContent = firstRun ? "Build this app" : "Your app will appear here";
  $("empty-description").textContent = firstRun
    ? "Describe what you want in the command box."
    : "Tell the agent what to build next in the command box.";
}

function eventPayload(element, action = element.dataset.action, draggedValue = "") {
  const fields = Object.create(null);
  for (const field of Array.from(generatedRoot.querySelectorAll("[data-field]")).slice(0, MAX_EVENT_FIELDS)) {
    const key = field.dataset.field;
    if (field.type === "checkbox" || field.type === "radio") fields[key] = Boolean(field.checked);
    else fields[key] = clipEncodedText(String(field.value || ""), MAX_EVENT_FIELD_BYTES);
  }
  const payload = {
    action,
    value: clipEncodedText(String(
      element.dataset.dropValue ?? ("value" in element ? element.value : "") ?? ""
    ), MAX_EVENT_FIELD_BYTES),
    checked: "checked" in element ? Boolean(element.checked) : false,
    draggedValue: clipEncodedText(String(draggedValue || ""), MAX_EVENT_FIELD_BYTES),
    fields,
  };
  if (jsonByteLength(payload) > MAX_EVENT_PAYLOAD_BYTES) {
    return { action, value: "", checked: false, draggedValue: "", fields: {} };
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

function appWritesBlocked() {
  return Boolean(pendingApp || selectedAppOutsideActiveIndex);
}

function generatedInteraction(event) {
  if (!(event.target instanceof Element)) return;
  const copyLink = event.target.closest("button[data-kern-copy-href]");
  if (copyLink && generatedRoot.contains(copyLink)) {
    event.preventDefault();
    requestHostCopy(copyLink.dataset.kernCopyHref || "")
      .then(() => showRuntimeStatus("Link copied", "success"))
      .catch(() => showRuntimeStatus("Could not copy link", "error"));
    return;
  }
  if (!selectedAppId || appWritesBlocked()) return;
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

function generatedEnterInteraction(event) {
  if (
    !selectedAppId || appWritesBlocked() || event.key !== "Enter" || event.shiftKey || event.altKey
    || event.ctrlKey || event.metaKey || event.repeat || event.isComposing
    || !(event.target instanceof Element)
  ) return;
  const target = event.target.closest("input[data-enter-action], textarea[data-enter-action]");
  if (!target || !generatedRoot.contains(target)) return;
  event.preventDefault();
  if (workerRun) {
    showRuntimeStatus("Finishing the previous app action");
    return;
  }
  const action = target.dataset.enterAction;
  runCapabilityWorker({ action, event: eventPayload(target, action) });
}

function setGeneratedDropTarget(target) {
  if (generatedDrag?.over === target) return;
  if (generatedDrag?.over) generatedDrag.over.removeAttribute("data-drag-over");
  if (target) target.setAttribute("data-drag-over", "true");
  if (generatedDrag) generatedDrag.over = target;
}

function clearGeneratedDrag() {
  if (!generatedDrag) return;
  generatedDrag.source.removeAttribute("data-dragging");
  setGeneratedDropTarget(null);
  generatedDrag = null;
}

function generatedDragStart(event) {
  if (!selectedAppId || appWritesBlocked() || !(event.target instanceof Element)) return;
  const source = event.target.closest("[data-drag-value]");
  if (!source || !generatedRoot.contains(source)) return;
  clearGeneratedDrag();
  generatedDrag = {
    source,
    over: null,
    value: clipEncodedText(source.dataset.dragValue || "", MAX_EVENT_FIELD_BYTES),
  };
  source.setAttribute("data-dragging", "true");
  if (event.dataTransfer) {
    // The trusted frame retains the bounded value. Keep DataTransfer empty so
    // generated data cannot leave the app through a cross-frame or OS drop.
    try {
      event.dataTransfer.clearData();
      event.dataTransfer.setData("text/plain", "");
      event.dataTransfer.effectAllowed = "move";
    } catch (_error) {
      // Some browsers expose a restricted DataTransfer; frame memory remains
      // the authoritative drag state.
    }
  }
}

function generatedDragOver(event) {
  if (appWritesBlocked()) {
    clearGeneratedDrag();
    return;
  }
  if (!generatedDrag || !(event.target instanceof Element)) return;
  const target = event.target.closest("[data-drop-action]");
  if (!target || !generatedRoot.contains(target)) {
    setGeneratedDropTarget(null);
    return;
  }
  event.preventDefault();
  setGeneratedDropTarget(target);
  if (event.dataTransfer) {
    try { event.dataTransfer.dropEffect = "move"; }
    catch (_error) { /* Restricted DataTransfer; trusted drag state still works. */ }
  }
}

function generatedDragLeave(event) {
  if (appWritesBlocked()) {
    clearGeneratedDrag();
    return;
  }
  if (!generatedDrag?.over) return;
  if (!(event.relatedTarget instanceof Node) || !generatedDrag.over.contains(event.relatedTarget)) {
    setGeneratedDropTarget(null);
  }
}

function generatedDrop(event) {
  if (appWritesBlocked()) {
    clearGeneratedDrag();
    return;
  }
  if (!generatedDrag || !(event.target instanceof Element)) return;
  const target = event.target.closest("[data-drop-action]");
  if (!target || !generatedRoot.contains(target)) return;
  event.preventDefault();
  const action = target.dataset.dropAction;
  const draggedValue = generatedDrag.value;
  clearGeneratedDrag();
  if (workerRun) {
    showRuntimeStatus("Finishing the previous app action");
    return;
  }
  runCapabilityWorker({
    action,
    event: eventPayload(target, action, draggedValue),
  });
}

// --- Capability worker lifecycle ---------------------------------------------
// Every turn still runs in a worker that is terminated when the turn ends —
// that contract is unchanged. Two things move off the interaction's critical
// path: the generated source is cached per UI revision, and after each
// completed turn the next worker is spawned and initialized ahead of time
// ("armed"), so a user event starts its handler immediately instead of paying
// spawn + parse + init round trips.

class SandboxedCapabilityWorker {
  constructor(source) {
    this.listeners = { message: new Set(), error: new Set() };
    this.bridge = new Worker("/workspace/capability-worker-sandbox.js");
    this.bridge.addEventListener("message", event => {
      const message = event.data;
      if (!message || typeof message !== "object") return;
      if (message.type === "capability-worker-message") this.dispatch("message", message.data);
      if (message.type === "capability-worker-error") this.dispatch("error", message);
    });
    this.bridge.addEventListener("error", event => {
      event.preventDefault();
      this.dispatch("error", { reason: "worker-create" });
    });
    this.bridge.postMessage({ type: "create", source });
  }

  addEventListener(type, listener) {
    this.listeners[type]?.add(listener);
  }

  postMessage(data) {
    this.bridge.postMessage({ type: "worker-post", data });
  }

  terminate() {
    this.bridge.terminate();
  }

  dispatch(type, data) {
    const event = type === "message"
      ? { data }
      : { preventDefault() {}, reason: data?.reason || "worker-runtime" };
    for (const listener of this.listeners[type] || []) listener(event);
  }
}

function workerSourceFor(appId, revision) {
  if (bundleUrl && bundleUrl.appId === appId && bundleUrl.revision === revision) {
    return bundleUrl.source;
  }
  revokeBundleUrl();
  const source = (
    `(${capabilityWorkerBootstrap.toString()})(${MAX_RENDER_HTML_BYTES},${MAX_RENDER_CSS_BYTES});\n`
    + `${snapshot.app.javascript}\n`
  );
  bundleUrl = { appId, revision, source };
  return source;
}

function revokeBundleUrl() {
  if (!bundleUrl) return;
  bundleUrl = null;
}

function discardArmedWorker() {
  if (!armedWorker) return;
  clearTimeout(armedWorker.timer);
  armedWorker.worker.terminate();
  armedWorker = null;
}

async function hydrateCapabilityData(holder, dataMode) {
  if (!["full", "targeted"].includes(dataMode)) return false;
  holder.dataMode = dataMode;
  if (dataMode === "targeted") holder.data = {};
  else if (holder.data === undefined) {
    let response;
    try {
      response = await api(
        "GET", `/apps/${encodeURIComponent(holder.appId)}/state/data`,
      );
    } catch (_error) {
      return false;
    }
    if (!response.app || response.app.revision !== holder.revision) {
      void refreshSelectedApp(holder.appId);
      return false;
    }
    holder.data = response.app.data;
  }
  if (snapshot.app?.revision === holder.revision && selectedAppId === holder.appId) {
    snapshot.app = { ...snapshot.app, data: holder.data, data_mode: dataMode };
  }
  return true;
}

function armCapabilityWorker() {
  if (
    !selectedAppId || appWritesBlocked() || workerRun
    || !snapshot.app || !snapshot.app.javascript
  ) return;
  const app = snapshot.app;
  if (
    armedWorker
    && armedWorker.appId === selectedAppId
    && armedWorker.revision === app.revision
  ) return;
  discardArmedWorker();
  const worker = new SandboxedCapabilityWorker(workerSourceFor(selectedAppId, app.revision));
  const armed = {
    worker,
    appId: selectedAppId,
    revision: app.revision,
    data: app.data,
    state: "arming",
    run: null,
    timer: null,
  };
  armedWorker = armed;
  const discard = () => {
    if (armedWorker === armed) discardArmedWorker();
    else if (!armed.run) {
      clearTimeout(armed.timer);
      worker.terminate();
    }
  };
  // Creating the worker may wait for the isolated sandbox iframe to finish
  // loading. Keep that startup bounded separately from generated execution.
  armed.timer = setTimeout(discard, WORKER_START_TIMEOUT_MS);
  worker.addEventListener("error", event => {
    event.preventDefault();
    if (armed.run) armed.run.finish("error");
    else discard();
  });
  worker.addEventListener("message", async event => {
    if (armed.run) {
      handleWorkerMessage(armed.run, event.data);
      return;
    }
    const message = event.data;
    if (!message || typeof message !== "object") {
      discard();
      return;
    }
    if (message.type === "ready" && armed.state === "arming") {
      if (!await hydrateCapabilityData(armed, message.data_mode)) {
        discard();
        return;
      }
      clearTimeout(armed.timer);
      armed.timer = setTimeout(discard, WORKER_TURN_TIMEOUT_MS);
      worker.postMessage({ type: "init", data: armed.data, load: false });
      return;
    }
    if (message.type === "initialized" && armed.state === "arming") {
      armed.state = "armed";
      clearTimeout(armed.timer);
      // Promise/queueMicrotask remain part of ordinary JavaScript and can
      // self-schedule without any exposed timer or channel. Keep a short idle
      // lifetime even after initialization so pre-arming is an optimization,
      // never a way for generated code to run in the background indefinitely.
      armed.timer = setTimeout(discard, WORKER_TURN_TIMEOUT_MS);
      return;
    }
    // Anything else from a worker that has no active turn — including a
    // spontaneous message while idle — is out of contract.
    discard();
  });
}

async function runCapabilityWorker(pendingEvent = null) {
  clearRuntimeStatus();
  if (appWritesBlocked() || !selectedAppId || !snapshot.app || !snapshot.app.javascript) return;
  if (workerRun) workerRun.finish("restarted");
  const appId = selectedAppId;
  const app = snapshot.app;
  let armed = null;
  if (
    pendingEvent
    && armedWorker
    && armedWorker.state === "armed"
    && armedWorker.appId === appId
    && armedWorker.revision === app.revision
  ) {
    armed = armedWorker;
    armedWorker = null;
  } else {
    discardArmedWorker();
  }
  const worker = armed
    ? armed.worker
    : new SandboxedCapabilityWorker(workerSourceFor(appId, app.revision));
  const run = {
    worker,
    appId,
    data: app.data,
    dataMode: app.data_mode || null,
    revision: app.revision,
    state: armed ? "event" : "starting",
    event: pendingEvent,
    count: 0,
    totalMessages: 0,
    mutations: 0,
    mutationPending: false,
    agentRequested: false,
    finished: false,
    windowStarted: performance.now(),
    timer: null,
    finish(reason, stage = this.state) {
      if (this.finished) return;
      this.finished = true;
      clearTimeout(this.timer);
      worker.terminate();
      const current = workerRun === this;
      if (current) workerRun = null;
      if (current && reason === "timeout" && stage === "starting") {
        showRuntimeStatus(
          "This app could not start its isolated renderer. Refresh and try again.",
          "error",
          true,
        );
      } else if (current && reason === "timeout") {
        showRuntimeStatus("This app action took too long and was stopped. Ask the agent to fix it.", "error");
      } else if (current && reason === "error" && stage === "render") {
        showRuntimeStatus(
          "This app could not render safely. Ask the agent to fix its interface.",
          "error",
          true,
        );
      } else if (current && reason === "error" && stage === "worker-create") {
        showRuntimeStatus(
          "This browser could not start the app sandbox. Refresh or update the browser.",
          "error",
          true,
        );
      } else if (current && reason === "error" && !this.event) {
        showRuntimeStatus(
          "This app could not start its live interface. Refresh and try again.",
          "error",
          true,
        );
      } else if (current && reason === "error") {
        showRuntimeStatus("This app action failed. Ask the agent to fix it.", "error");
      }
      if (current && reason === "complete") setTimeout(armCapabilityWorker, 0);
    },
  };
  workerRun = run;
  run.timer = setTimeout(() => run.finish("timeout", "starting"), WORKER_START_TIMEOUT_MS);
  if (armed) {
    clearTimeout(armed.timer);
    clearTimeout(run.timer);
    run.timer = setTimeout(() => run.finish("timeout", "execution"), WORKER_TURN_TIMEOUT_MS);
    armed.run = run;
    worker.postMessage({
      type: "event", action: pendingEvent.action, event: pendingEvent.event, turn_id: "turn",
    });
    return;
  }
  worker.addEventListener("error", event => {
    event.preventDefault();
    run.finish("error", event.reason);
  });
  worker.addEventListener("message", event => handleWorkerMessage(run, event.data));
}

async function handleWorkerMessage(run, message) {
  if (
    workerRun !== run || appWritesBlocked() || selectedAppId !== run.appId
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
    if (!await hydrateCapabilityData(run, message.data_mode)) {
      run.finish("error", "data-load");
      return;
    }
    clearTimeout(run.timer);
    run.timer = setTimeout(() => run.finish("timeout", "execution"), WORKER_TURN_TIMEOUT_MS);
    run.state = "initializing";
    run.worker.postMessage({ type: "init", data: run.data, load: !run.event });
    return;
  }
  if (message.type === "initialization-error" && run.state === "initializing") {
    run.finish("error", message.reason || "initializing");
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
    catch (_error) { run.finish("error", "render"); }
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
    void sendMessage(message.message.trim(), run.appId);
    return;
  }
  if (message.type === "data-action") await handleWorkerDataAction(run, message);
  if (message.type === "data-read") await handleWorkerDataRead(run, message);
  if (message.type === "collection-query") await handleWorkerCollectionQuery(run, message);
}

function applyLocalDataAction(data, message) {
  const updated = structuredClone(data);
  let parent = updated;
  for (const segment of message.path.slice(0, -1)) parent = parent[segment];
  const leaf = message.path[message.path.length - 1];
  if (message.action === "append") parent[leaf].push(structuredClone(message.value));
  else if (message.action === "delete") {
    if (Array.isArray(parent)) parent.splice(leaf, 1);
    else delete parent[leaf];
  } else if (Array.isArray(parent)) parent[leaf] = structuredClone(message.value);
  else Object.defineProperty(parent, leaf, {
    value: structuredClone(message.value),
    writable: true,
    enumerable: true,
    configurable: true,
  });
  return updated;
}

async function handleWorkerDataRead(run, message) {
  if (
    !["initializing", "event"].includes(run.state)
    || typeof message.request_id !== "string"
    || !/^read-[1-9][0-9]{0,8}$/.test(message.request_id)
    || !validDataPath(message.path)
  ) {
    run.finish("error");
    return;
  }
  try {
    const response = await api(
      "POST",
      `/apps/${encodeURIComponent(run.appId)}/runtime/data/read`,
      { path: message.path },
    );
    if (workerRun !== run || selectedAppId !== run.appId) return;
    if (!response.app || response.app.revision !== run.revision) {
      run.worker.postMessage({ type: "read-result", request_id: message.request_id, ok: false });
      await refreshSelectedApp(run.appId);
      return;
    }
    run.worker.postMessage({
      type: "read-result",
      request_id: message.request_id,
      ok: true,
      value: response.app.value,
    });
  } catch (_error) {
    if (workerRun !== run) return;
    run.worker.postMessage({ type: "read-result", request_id: message.request_id, ok: false });
  }
}

async function handleWorkerCollectionQuery(run, message) {
  if (
    !["initializing", "event"].includes(run.state)
    || typeof message.request_id !== "string"
    || !/^query-[1-9][0-9]{0,8}$/.test(message.request_id)
    || typeof message.collection !== "string"
    || !/^[a-z][a-z0-9_-]{0,63}$/.test(message.collection)
    || !message.query || typeof message.query !== "object" || Array.isArray(message.query)
    || jsonByteLength(message.query) < 0
    || jsonByteLength(message.query) > MAX_DATA_VALUE_BYTES
  ) {
    run.finish("error");
    return;
  }
  try {
    const response = await api(
      "POST",
      `/apps/${encodeURIComponent(run.appId)}/runtime/collections/${encodeURIComponent(message.collection)}/query`,
      message.query,
    );
    if (workerRun !== run || selectedAppId !== run.appId) return;
    if (!response.collection || response.collection.revision !== run.revision) {
      run.worker.postMessage({
        type: "collection-query-result",
        request_id: message.request_id,
        ok: false,
      });
      await refreshSelectedApp(run.appId);
      return;
    }
    run.worker.postMessage({
      type: "collection-query-result",
      request_id: message.request_id,
      ok: true,
      collection: response.collection,
    });
  } catch (_error) {
    if (workerRun !== run) return;
    run.worker.postMessage({
      type: "collection-query-result",
      request_id: message.request_id,
      ok: false,
    });
  }
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
  let canonicalValue;
  if (message.action !== "delete") {
    try {
      // The HTTP hop serializes JSON, so apply and acknowledge exactly the
      // normalized value the backend receives (Date -> string, NaN -> null,
      // Map -> object, and so on), never the pre-serialization worker value.
      canonicalValue = JSON.parse(JSON.stringify(message.value));
    } catch (_error) {
      run.finish("error");
      return;
    }
    body.value = canonicalValue;
  }
  try {
    const response = await api(
      "POST",
      `/apps/${encodeURIComponent(run.appId)}/runtime/actions`,
      body,
    );
    if (workerRun !== run || selectedAppId !== run.appId) return;
    const acknowledged = { ...message, value: canonicalValue };
    const updatedData = run.dataMode === "full"
      ? applyLocalDataAction(run.data, acknowledged)
      : run.data;
    snapshot.app = {
      ...snapshot.app,
      data: updatedData,
      revision: response.app.revision,
      updated_at: response.app.updated_at,
    };
    run.data = updatedData;
    run.revision = response.app.revision;
    renderedRevision = response.app.revision;
    // A successful generated-App write used the displayed revision, so this
    // canvas is already authoritative and stays interactive.
    pendingApp = null;
    syncAppRefreshButton();
    markSelectedAppSeen();
    run.mutationPending = false;
    run.worker.postMessage({
      type: "data-result",
      request_id: message.request_id,
      ok: true,
      value: canonicalValue,
    });
  } catch (_error) {
    if (workerRun !== run) return;
    run.mutationPending = false;
    run.worker.postMessage({ type: "data-result", request_id: message.request_id, ok: false });
    await refreshSelectedApp(run.appId);
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

function clearRuntimeStatus() {
  runtimeStatusSequence += 1;
  $("runtime-status").hidden = true;
  $("builder-shell").classList.remove("runtime-status-visible");
}

function showRuntimeStatus(message, level = "info", persistent = false) {
  const status = $("runtime-status");
  const sequence = ++runtimeStatusSequence;
  status.textContent = message;
  status.className = `runtime-status ${level}`;
  status.hidden = false;
  $("builder-shell").classList.add("runtime-status-visible");
  if (persistent) return;
  setTimeout(() => {
    if (runtimeStatusSequence !== sequence) return;
    status.hidden = true;
    $("builder-shell").classList.remove("runtime-status-visible");
  }, 4500);
}

// --- Contextual settings and recovery ---------------------------------------

function setSettingsOpen(open) {
  const expanded = Boolean(open && selectedAppId);
  $("settings-popover").hidden = !expanded;
  $("settings-open").setAttribute("aria-expanded", String(expanded));
}

function setRecoveryOpen(open) {
  const expanded = Boolean(open && selectedAppId);
  $("recovery-drawer").hidden = !expanded;
  $("recovery-backdrop").hidden = !expanded;
  $("recovery-open").setAttribute("aria-expanded", String(expanded));
  if (expanded) void refreshAdminPanel(true);
}

function closeAdmin() {
  setSettingsOpen(false);
  setRecoveryOpen(false);
}

async function refreshAdminPanel(opened = false) {
  if ($("recovery-drawer").hidden || !selectedAppId) return;
  const sequence = ++panelRefreshSequence;
  const appId = selectedAppId;
  const panel = $("recovery-drawer");
  panel.setAttribute("aria-busy", "true");
  if (opened) showPanelLoading("history");
  try {
    const response = await api("GET", `/apps/${encodeURIComponent(appId)}/revisions`);
    if (sequence !== panelRefreshSequence || appId !== selectedAppId) return;
    recoveryPoints = response.revisions || [];
    renderRecovery();
  } catch (error) {
    if (
      opened
      && sequence === panelRefreshSequence
      && appId === selectedAppId
    ) showRuntimeStatus(error.message || "Could not load this panel", "error");
  } finally {
    if (sequence === panelRefreshSequence && appId === selectedAppId) {
      panel.removeAttribute("aria-busy");
    }
  }
}

function showPanelLoading(tab) {
  const targets = {
    history: ["app-history-list", "Loading revisions…"],
  };
  const target = targets[tab];
  if (!target) return;
  const container = $(target[0]);
  if (container.children.length) return;
  const loading = document.createElement("p");
  loading.className = "panel-empty panel-loading";
  loading.setAttribute("role", "status");
  loading.textContent = target[1];
  container.append(loading);
}

// --- Recovery panel ----------------------------------------------------------

function historyIcon(kind) {
  if (kind === "ui") {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="4" y="4" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4 8.5h12" stroke="currentColor" stroke-width="1.5"/></svg>';
  }
  if (kind === "restore") {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6 4.5v11l4-2.5 4 2.5v-11z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  }
  return '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3" fill="currentColor"/></svg>';
}

function renderRecovery() {
  const list = $("app-history-list");
  list.replaceChildren();
  if (!recoveryPoints.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = "No earlier revisions are available.";
    list.append(empty);
  }
  recoveryPoints.forEach(entry => {
    const item = document.createElement("article");
    item.className = `history-item history-${entry.kind}`;

    const icon = document.createElement("span");
    icon.className = "history-icon";
    icon.innerHTML = historyIcon(entry.kind);

    const body = document.createElement("div");
    body.className = "history-body";
    const resource = document.createElement("span");
    resource.className = "history-resource";
    resource.textContent = entry.kind === "restore" ? "Restored" : "Revision";
    const summary = document.createElement("span");
    summary.className = "history-summary";
    summary.textContent = `Revision ${entry.revision} · ${entry.actor}`;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    meta.textContent = new Date(entry.created_at).toLocaleString();
    meta.title = `Whole-App revision ${entry.revision}`;
    body.append(resource, summary, meta);

    item.append(icon, body);
    const revert = document.createElement("button");
    revert.className = "ghost sm history-revert";
    revert.dataset.restoreRevision = String(entry.revision);
    revert.textContent = "Restore";
    revert.disabled = selectedAppOutsideActiveIndex;
    if (selectedAppOutsideActiveIndex) {
      revert.title = "Restore this app from the Apps sidebar before recovering a revision";
    }
    item.append(revert);
    list.append(item);
  });
}

async function restoreRevision(revision) {
  if (!selectedAppId || selectedAppOutsideActiveIndex) return;
  const entry = recoveryPoints.find(candidate => candidate.revision === revision);
  if (!entry || !confirm(`Restore interface and data from revision ${revision}?`)) return;
  const appId = selectedAppId;
  try {
    const response = await api(
      "POST",
      `/apps/${encodeURIComponent(appId)}/revisions/${revision}/restore`,
      {},
      AGENT_DELIVERY_TIMEOUT_MS,
    );
    if (selectedAppId !== appId) return;
    // Revert is an explicit human choice, so its returned version is applied
    // immediately instead of being treated like a background agent update.
    applyAppVersion(response.app);
    showRuntimeStatus("App restored", "success");
    await refresh();
    await refreshAdminPanel(true);
  } catch (error) {
    if (selectedAppId === appId) {
      showRuntimeStatus(error.message || "Could not restore this revision", "error");
    }
  }
}

// --- Chat --------------------------------------------------------------------

function showChatStatus(message, error = false) {
  transientAgentStatus = message
    ? { key: "transient", kind: error ? "error" : "agent", message }
    : null;
  renderChat();
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

function setAttachmentActivity(appId, activity) {
  if (activity === null) attachmentActivities.delete(appId);
  else attachmentActivities.set(appId, activity);
  renderAttachments();
}

async function attachFile() {
  const appId = selectedAppId;
  const remaining = ATTACHMENT_LIMIT - pendingAttachments.length;
  if (
    !appId
    || remaining <= 0
    || messageBusyApps.has(appId)
  ) return;
  showChatStatus("");
  setAttachmentActivity(appId, "Selecting file…");
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
    if (selectedAppId !== appId) {
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
    setAttachmentActivity(appId, null);
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
  // Pointer activation already respects the disabled button. Match that
  // behavior for Enter and any other programmatic composer submission while
  // attachments or session options make the draft invalid.
  if (selectedAppOutsideActiveIndex || (!fromGeneratedApp && $("send-message").disabled)) return;
  const appId = targetAppId || selectedAppId;
  const submittedDraft = fromGeneratedApp ? null : $("message").value;
  const message = (fromGeneratedApp ? forcedMessage : submittedDraft).trim();
  const attachments = fromGeneratedApp ? [] : pendingAttachments;
  if (
    (!message && !attachments.length)
    || !appId
    || appId !== selectedAppId
  ) return;
  if (messageBusyApps.has(appId)) {
    if (fromGeneratedApp) showRuntimeStatus("Agent is already starting");
    return;
  }
  sessionAgentMessageApps.add(appId);
  if (!fromGeneratedApp) saveComposerDraft();
  setDismissedAgentMessage(appId, conversationEntries()
    .filter(entry => ["agent", "error", "stopped"].includes(entry.kind))
    .at(-1)?.key || null);
  $("latest-agent-card").hidden = true;
  messageBusyApps.add(appId);
  setSessionOptions();
  showChatStatus("");
  if (fromGeneratedApp) showRuntimeStatus("Sending to agent…");
  try {
    for (const [index, attachment] of attachments.entries()) {
      if (attachment.file) continue;
      setAttachmentActivity(
        appId,
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
    setAttachmentActivity(appId, null);
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
      `/apps/${encodeURIComponent(appId)}/${resource}`,
      body,
      AGENT_DELIVERY_TIMEOUT_MS,
    );
    if (!fromGeneratedApp && selectedAppId === appId) {
      const clearedSubmittedDraft = clearComposerDraft(appId, submittedDraft);
      if (clearedSubmittedDraft && $("message").value === submittedDraft) {
        $("message").value = "";
      }
      pendingAttachments = [];
      renderAttachments();
    } else if (!fromGeneratedApp) {
      clearComposerDraft(appId, submittedDraft);
    }
    await refreshSelectedApp(appId);
    if (selectedAppId !== appId) return;
    showChatStatus("");
    if (fromGeneratedApp) showRuntimeStatus("Sent to agent", "success");
  } catch (error) {
    setAttachmentActivity(appId, null);
    if (selectedAppId !== appId) {
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
    messageBusyApps.delete(appId);
    if (selectedAppId === appId) setSessionOptions();
  }
}

async function stopRunningTurn() {
  const appId = selectedAppId;
  if (!appId || snapshot.status !== "running") return;
  if (!confirm("Stop the agent?")) return;
  showChatStatus("Stopping…");
  try {
    await api(
      "POST",
      `/apps/${encodeURIComponent(appId)}/stop`,
      {},
      AGENT_DELIVERY_TIMEOUT_MS,
    );
  } finally {
    if (selectedAppId === appId) showChatStatus("");
  }
  await refreshSelectedApp(appId);
}

function renderChat() {
  const running = snapshot.status === "running";
  let latest = transientAgentStatus;
  if (!latest && selectedAppId && sessionAgentMessageApps.has(selectedAppId)) {
    latest = conversationEntries()
      .filter(entry => ["agent", "error", "stopped"].includes(entry.kind))
      .at(-1);
  }
  if (running && (!latest || latest.key === dismissedAgentMessageKey)) {
    latest = { key: "running", kind: "agent", message: "Agent is working" };
  }
  if (historyMode) {
    latest = running
      ? { key: "running", kind: "agent", message: "Agent is working" }
      : null;
  }
  const card = $("latest-agent-card");
  if (!latest || latest.key === dismissedAgentMessageKey) {
    card.hidden = true;
  } else {
    $("latest-agent-kind").className = `latest-agent-kind ${latest.kind}`;
    $("latest-agent-kind").textContent = latest.kind === "error" ? "!" : "✦";
    $("latest-agent-message").textContent = latest.message;
    card.className = `latest-agent-card ${latest.kind}`;
    card.hidden = false;
  }
  $("stop-turn").hidden = !running;
  $("latest-agent-dismiss").hidden = running;
  renderConversationHistory();
  syncAgentSettings(snapshot.session);
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
      const fromUser = payload.source === "user";
      entries.push({
        key: `event-${event.seq}`,
        kind: fromUser ? "user" : "agent",
        message: payload.message,
      });
    }
  }
  return entries;
}

function renderConversationHistory(forceBottom = false) {
  const scroll = $("chat-history-scroll");
  const list = $("chat-history-list");
  const appChanged = historyRenderedAppId !== selectedAppId;
  const wasNearBottom = appChanged || (
    scroll.scrollHeight - scroll.scrollTop - scroll.clientHeight < 48
  );
  const entries = conversationEntries();
  const entryKey = entries.map(entry => entry.key).join("\0");
  if (appChanged || entryKey !== historyRenderedEntryKey) {
    const nodes = entries.map(entry => {
      const item = document.createElement("article");
      item.className = `chat-history-entry ${entry.kind}`;
      const author = document.createElement("strong");
      author.className = "chat-history-author";
      author.textContent = entry.kind === "user"
        ? "You"
        : entry.kind === "agent"
          ? "Agent"
          : "System";
      const message = document.createElement("div");
      message.className = "chat-history-message";
      // Thread messages are intentionally shown exactly as recorded. This keeps
      // host-added Web App context visible instead of silently rewriting history.
      message.textContent = entry.message;
      item.append(author, message);
      return item;
    });
    list.replaceChildren(...nodes);
    historyRenderedAppId = selectedAppId;
    historyRenderedEntryKey = entryKey;
  }
  $("chat-history-empty").hidden = entries.length !== 0;
  const pageState = activeConversationEventPage();
  const more = $("chat-history-more");
  more.hidden = !pageState.hasOlder;
  more.disabled = historyLoadingOlder;
  more.textContent = historyLoadingOlder
    ? "Loading earlier messages…"
    : "Load earlier messages";
  if (historyMode && (forceBottom || appChanged || wasNearBottom)) {
    scroll.scrollTop = scroll.scrollHeight;
  }
}

async function loadOlderConversationEvents() {
  const pageState = activeConversationEventPage();
  if (
    historyLoadingOlder || !historyMode || !selectedAppId
    || !pageState.hasOlder || pageState.oldestSeq === null
  ) return;
  const appId = selectedAppId;
  const scroll = $("chat-history-scroll");
  historyLoadingOlder = true;
  renderConversationHistory();
  try {
    const response = await api(
      "GET",
      conversationEventsPath(appId, pageState, "before", pageState.oldestSeq),
    );
    if (
      selectedAppId !== appId
      || activeConversationEventPage() !== pageState
      || !historyMode
    ) return;
    const events = response.events || [];
    const older = events.filter(event => event.seq < pageState.oldestSeq);
    const previousHeight = scroll.scrollHeight;
    const previousTop = scroll.scrollTop;
    if (older.length) {
      mergeConversationEvents(older);
      pageState.oldestSeq = older[0].seq;
    }
    pageState.hasOlder = events.length === CONVERSATION_EVENTS_PAGE;
    renderConversationHistory();
    scroll.scrollTop = previousTop + scroll.scrollHeight - previousHeight;
  } catch (error) {
    if (selectedAppId === appId) {
      showRuntimeStatus(error.message || "Could not load earlier messages", "error");
    }
  } finally {
    historyLoadingOlder = false;
    if (selectedAppId === appId && historyMode) renderConversationHistory();
  }
}

function setHistoryMode(open) {
  historyMode = Boolean(open && selectedAppId);
  const toggle = $("history-toggle");
  toggle.classList.toggle("active", historyMode);
  toggle.setAttribute("aria-pressed", String(historyMode));
  toggle.setAttribute("aria-label", historyMode ? "Show app" : "Show chat history");
  toggle.title = historyMode ? "Show app" : "Show chat history";
  toggle.querySelector("span").textContent = historyMode ? "App" : "History";
  $("chat-history").hidden = !historyMode;
  if (historyMode) {
    closeAdmin();
    renderConversationHistory(true);
  }
  renderChat();
}

function mergeConversationEvents(events) {
  const bySeq = new Map(conversationEvents.map(event => [event.seq, event]));
  for (const event of events) bySeq.set(event.seq, event);
  const ordered = Array.from(bySeq.values()).sort((a, b) => a.seq - b.seq);
  conversationEvents = KernRichText.compactActivityEvents(ordered);
}

function freshConversationEventPages() {
  const page = () => ({
    oldestSeq: null,
    newestSeq: 0,
    initialized: false,
    hasOlder: false,
  });
  return { all: page(), conversation: page() };
}

function activeConversationEventPage() {
  return conversationEventPages.conversation;
}

function conversationEventsPath(appId, pageState, cursorName = null, cursor = null) {
  const query = [];
  if (pageState === conversationEventPages.conversation) query.push("activity=false");
  if (cursorName !== null) query.push(`${cursorName}=${cursor}`);
  const suffix = query.length ? `?${query.join("&")}` : "";
  return `/apps/${encodeURIComponent(appId)}/conversation/events${suffix}`;
}

async function refreshConversationEvents(appId, refreshSequence) {
  const pageState = activeConversationEventPage();
  if (!pageState.initialized) {
    const response = await api(
      "GET",
      conversationEventsPath(appId, pageState),
    );
    if (selectedAppId !== appId || selectedRefreshSequence !== refreshSequence) return;
    const events = response.events || [];
    mergeConversationEvents(events);
    if (events.length) {
      pageState.oldestSeq = events[0].seq;
      pageState.newestSeq = events[events.length - 1].seq;
    }
    let oldestPage = events;
    for (
      let page = 1;
      page < INITIAL_CONVERSATION_EVENT_PAGES
      && oldestPage.length === CONVERSATION_EVENTS_PAGE
      && pageState.oldestSeq !== null;
      page += 1
    ) {
      const before = pageState.oldestSeq;
      const olderResponse = await api(
        "GET",
        conversationEventsPath(appId, pageState, "before", before),
      );
      if (selectedAppId !== appId || selectedRefreshSequence !== refreshSequence) return;
      oldestPage = (olderResponse.events || []).filter(event => event.seq < before);
      if (oldestPage.length) {
        mergeConversationEvents(oldestPage);
        pageState.oldestSeq = oldestPage[0].seq;
      }
    }
    pageState.hasOlder = oldestPage.length === CONVERSATION_EVENTS_PAGE;
    pageState.initialized = true;
    return;
  }
  for (;;) {
    const since = pageState.newestSeq;
    const response = await api(
      "GET",
      conversationEventsPath(appId, pageState, "since", since),
    );
    if (selectedAppId !== appId || selectedRefreshSequence !== refreshSequence) return;
    const events = response.events || [];
    const fresh = events.filter(event => event.seq > since);
    if (fresh.length) {
      mergeConversationEvents(fresh);
      pageState.newestSeq = fresh[fresh.length - 1].seq;
      if (pageState.oldestSeq === null) pageState.oldestSeq = fresh[0].seq;
    }
    if (fresh.length < CONVERSATION_EVENTS_PAGE) return;
  }
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
  const settingsLocked = activeSettingsLock;
  runtimeSelect.disabled = settingsLocked;
  modelSelect.disabled = settingsLocked || !modelSelect.value;
  effortSelect.disabled = settingsLocked || !effortSelect.value;
  $("agent-settings").classList.toggle("active-locked", activeSettingsLock);
  if (!activeSettingsLock) {
    $("agent-settings").classList.remove("show-lock-note");
  }
  $("agent-session-change-warning").hidden = (
    settingsLocked || !sessionConfigurationChanged()
  );
  const activeBlock = running && establishedSession?.agent_runtime === "hermes";
  const hasOversizedAttachment = pendingAttachments.some(
    attachment => attachment.size_bytes > ATTACHMENT_MAX_BYTES,
  );
  const attachmentBusy = attachmentActivities.has(selectedAppId);
  $("message").disabled = activeBlock;
  $("message").placeholder = running
    ? activeBlock
      ? "Hermes does not support follow-ups while running"
      : "Send another message"
    : "Describe the app or ask about its data";
  $("send-message").disabled = (
    !selectedAppId || messageBusyApps.has(selectedAppId)
    || activeBlock
    || attachmentBusy
    || hasOversizedAttachment
    || !modelSelect.value
    || !effortSelect.value
    // A deactivated runtime cannot run the message, so sending it would only
    // surface a host rejection after the fact.
    || !runtimeRunnable($("runtime").value)
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
    || activeBlock
    || attachmentBusy
    || pendingAttachments.length >= ATTACHMENT_LIMIT
  );
}

// Two different questions. Null active runtimes means the host could not
// report activation, so neither gate applies: an unknown status must never
// hide a usable provider or block sending.

// Can it be shown as the selection? Only a session the app actually ran with
// keeps its runtime here after that provider is turned off, so the settings
// still show the truth. An unconfigured app has no such claim: it opens on a
// fallback nobody chose, which must not be dressed up as usable.
function runtimeSelectable(runtime) {
  if (!Array.isArray(activeRuntimes)) return true;
  if (establishedSession && runtime === establishedSession.agent_runtime) return true;
  return activeRuntimes.includes(runtime);
}

// Can the host actually run it? A deactivated runtime is refused on admission,
// so an established session gets no exemption here: displaying its recorded
// configuration is honest, offering to send another message on it is not.
function runtimeRunnable(runtime) {
  if (!Array.isArray(activeRuntimes)) return true;
  return activeRuntimes.includes(runtime);
}

function setRuntimeOptions(preferredRuntime = null) {
  const labels = { codex: "Codex", claude_code: "Claude Code", hermes: "Hermes" };
  const current = preferredRuntime || $("runtime").value;
  const runtimes = Object.keys(sessionOptions);
  if (current && !runtimes.includes(current)) runtimes.push(current);
  $("runtime").replaceChildren(...runtimes.map(value => {
    const label = labels[value] || value;
    const available = runtimeSelectable(value);
    const option = new Option(available ? label : `${label} (not activated)`, value);
    option.disabled = !available;
    return option;
  }));
  if (runtimes.includes(current)) $("runtime").value = current;
}

function defaultSessionConfig() {
  const offered = Object.keys(sessionOptions);
  const runtime = offered.find(runtimeRunnable) || offered[0] || "";
  const models = sessionOptions[runtime] || {};
  const model = Object.keys(models)[0] || "";
  const effort = (models[model] || [])[0] || "";
  return { agent_runtime: runtime, model, effort };
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
  // An app with no session is unconfigured, and opens on the first offered
  // configuration the way Agent Chat opens a new thread. Leaving the
  // selectors as they stand would carry the previously opened app's runtime
  // and model into this one, so a first message sent without visiting the
  // settings would start a session the operator never chose.
  const opening = next || defaultSessionConfig();
  setRuntimeOptions(opening.agent_runtime || null);
  setSessionOptions(opening.model || null, opening.effort || null);
}

function syncWorkspaceControls() {
  const hasApp = selectedAppId !== null;
  const readOnly = hasApp && selectedAppOutsideActiveIndex;
  if (!hasApp) historyMode = false;
  $("app-title").textContent = selectedAppName || "";
  $("app-view-toolbar").hidden = !hasApp;
  $("agent-command-surface").hidden = !hasApp || readOnly;
  $("chat-composer").hidden = !hasApp || readOnly;
  $("rename-app").disabled = !hasApp || readOnly;
  $("settings-open").disabled = !hasApp || readOnly;
  const historyToggle = $("history-toggle");
  historyToggle.disabled = !hasApp;
  historyToggle.classList.toggle("active", historyMode);
  historyToggle.setAttribute("aria-pressed", String(historyMode));
  historyToggle.setAttribute("aria-label", historyMode ? "Show app" : "Show chat history");
  historyToggle.title = historyMode ? "Show app" : "Show chat history";
  historyToggle.querySelector("span").textContent = historyMode ? "App" : "History";
  $("chat-history").hidden = !historyMode;
  const lockButton = $("lock-agent-updates");
  lockButton.disabled = !hasApp || readOnly || agentUpdateLockBusy;
  lockButton.classList.toggle("active", selectedAgentUpdatesLocked);
  lockButton.setAttribute("aria-pressed", String(selectedAgentUpdatesLocked));
  lockButton.setAttribute(
    "aria-label",
    selectedAgentUpdatesLocked ? "Unlock agent updates" : "Lock agent updates",
  );
  lockButton.title = selectedAgentUpdatesLocked
    ? "Allow agents to change this app again"
    : "Temporarily stop agents from changing this app";
  lockButton.querySelector("span").textContent = selectedAgentUpdatesLocked ? "Unlock" : "Lock";
  $("archive-app").disabled = !hasApp || readOnly || snapshot.status === "running";
  $("archive-app").querySelector("span").textContent = readOnly ? "Archived" : "Archive";
  $("archive-app").setAttribute("aria-label", readOnly ? "Archived app" : "Archive app");
  $("archive-app").title = readOnly
    ? "Restore this app from the Apps sidebar before editing"
    : snapshot.status === "running"
      ? "Stop the agent before archiving this app"
      : "Archive app";
  $("archived-app-veil").hidden = !readOnly;
  syncAppRefreshButton();
  setSessionOptions();
  syncCanvasState();
}

function stopCapabilityWorker() {
  if (workerRun) workerRun.finish("workspace-switched");
  discardArmedWorker();
}

function hasAppBundle(app) {
  return Boolean(app && (app.html || app.css || app.javascript));
}

function compareAppVersions(left, right) {
  if (!left) return right ? -1 : 0;
  if (!right) return 1;
  return left.revision - right.revision;
}

function appMutationInFlight(appId, currentRevision, observedRevision) {
  return Boolean(
    workerRun?.appId === appId
    && workerRun.mutationPending
    && workerRun.revision === currentRevision
    && observedRevision === currentRevision + 1
  );
}

function syncAppRefreshButton() {
  $("app-update-veil").hidden = !selectedAppId || !pendingApp || selectedAppOutsideActiveIndex;
  if (!pendingApp) return;
  const focused = generatedRoot?.activeElement;
  if (focused && typeof focused.blur === "function") focused.blur();
  clearGeneratedDrag();
  stopCapabilityWorker();
}

function applyAppVersion(app) {
  if (!app) return;
  const changed = app.revision !== renderedRevision;
  const hasBundle = hasAppBundle(app);
  stopCapabilityWorker();
  snapshot.app = app;
  pendingApp = null;
  renderedRevision = app.revision;
  if (hasBundle) {
    if (changed) {
      try { renderGenerated(app.html, app.css); }
      catch (_error) {
        // The dynamic onLoad render may still recover from a bad stored
        // placeholder. Clear the old revision so it cannot remain interactive
        // against the newly accepted revision while worker startup proceeds.
        clearGenerated();
        showRuntimeStatus("The saved app preview could not render; starting its live interface…", "error");
      }
    }
    if (changed) runCapabilityWorker();
  } else {
    clearGenerated();
  }
  syncAppRefreshButton();
  armCapabilityWorker();
}

function applyPendingAppVersion() {
  if (!pendingApp) return;
  applyAppVersion(pendingApp);
  markSelectedAppSeen();
}

function markSelectedAppSeen() {
  if (!selectedAppId || renderedRevision < 0) return;
  const listed = apps.find(app => app.app_id === selectedAppId);
  const renderedMessageSeq = conversationEvents.reduce((latest, event) => (
    event.event_type === "thread.message"
      ? Math.max(latest, Number(event.seq) || 0)
      : latest
  ), 0);
  window.KernHost.markWorkspaceSeen("apps", {
    app_id: selectedAppId,
    last_used_at: listed?.last_used_at || snapshot.app?.updated_at || "",
    latest_message_seq: Math.max(
      Number(listed?.latest_message_seq) || 0,
      renderedMessageSeq,
    ),
    revision: renderedRevision,
  });
}

function saveSelectedConversationView() {
  if (!selectedAppId) return;
  // Refresh insertion order so the oldest unvisited app is evicted first.
  conversationViewStates.delete(selectedAppId);
  conversationViewStates.set(selectedAppId, {
    session: snapshot.session,
    status: snapshot.status,
    events: conversationEvents,
    eventPages: {
      all: { ...conversationEventPages.all },
      conversation: { ...conversationEventPages.conversation },
    },
  });
  if (conversationViewStates.size > VIEW_STATE_LIMIT) {
    conversationViewStates.delete(conversationViewStates.keys().next().value);
  }
}

function restoreConversationView(app) {
  const state = conversationViewStates.get(app.app_id);
  if (!state) {
    snapshot = { app: null, session: app.session || null, status: app.status || "idle" };
    conversationEvents = [];
    conversationEventPages = freshConversationEventPages();
  } else {
    snapshot = {
      app: null,
      session: app.session || state.session || null,
      status: app.status || state.status || "idle",
    };
    conversationEvents = state.events;
    conversationEventPages = {
      all: { ...state.eventPages.all },
      conversation: { ...state.eventPages.conversation },
    };
  }
}

function clearSelectedApp() {
  saveComposerDraft();
  saveSelectedConversationView();
  clearPendingAttachments();
  stopCapabilityWorker();
  revokeBundleUrl();
  appSelectionSequence += 1;
  selectedRefreshSequence += 1;
  panelRefreshSequence += 1;
  if (selectedAppId) sessionAgentMessageApps.delete(selectedAppId);
  selectedAppId = null;
  selectedAppName = null;
  selectedAgentUpdatesLocked = false;
  agentUpdateLockBusy = false;
  selectedAppOutsideActiveIndex = false;
  recoveryPoints = [];
  dismissedAgentMessageKey = null;
  transientAgentStatus = null;
  restoreComposerDraft();
  snapshot = { app: null, session: null, status: "idle" };
  conversationEvents = [];
  conversationEventPages = freshConversationEventPages();
  renderedRevision = -1;
  pendingApp = null;
  establishedSession = null;
  establishedSessionKey = "";
  clearGenerated();
  closeAdmin();
  renderChat();
  syncWorkspaceControls();
  return true;
}

async function showApp(app, outsideActiveIndex = false, updateHistory = true) {
  saveComposerDraft();
  saveSelectedConversationView();
  appSelectionSequence += 1;
  if (selectedAppId !== app.app_id) clearPendingAttachments();
  stopCapabilityWorker();
  revokeBundleUrl();
  selectedRefreshSequence += 1;
  panelRefreshSequence += 1;
  sessionAgentMessageApps.delete(app.app_id);
  selectedAppId = app.app_id;
  selectedAppName = app.name;
  selectedAgentUpdatesLocked = Boolean(app.agent_updates_locked);
  selectedAppOutsideActiveIndex = outsideActiveIndex;
  if (updateHistory) window.KernHost.navigateWorkspace("apps", app.app_id);
  recoveryPoints = [];
  dismissedAgentMessageKey = dismissedAgentMessages[app.app_id] || null;
  transientAgentStatus = null;
  restoreComposerDraft();
  restoreConversationView(app);
  renderedRevision = -1;
  pendingApp = null;
  establishedSession = null;
  establishedSessionKey = "";
  clearGenerated();
  closeAdmin();
  renderChat();
  syncWorkspaceControls();
  await refreshSelectedApp(app.app_id);
}

async function refreshSelectedApp(appId = selectedAppId) {
  if (!appId || appId !== selectedAppId) return;
  const refreshSequence = ++selectedRefreshSequence;
  const [stateResponse, conversationResponse] = await Promise.all([
    api("GET", `/apps/${encodeURIComponent(appId)}/state/ui`),
    api("GET", `/apps/${encodeURIComponent(appId)}/conversation`),
  ]);
  if (appId !== selectedAppId || selectedRefreshSequence !== refreshSequence) return;
  const listedSession = apps.find(app => app.app_id === appId)?.session || null;
  const next = {
    app: stateResponse.app,
    // A fixed host session outlives retained history. The app index reads it
    // from the host thread summary, so an empty conversation response must
    // not make an established workspace look configurable again.
    session: conversationResponse.session || listedSession || snapshot.session || null,
    status: conversationResponse.status || "idle",
  };
  selectedAgentUpdatesLocked = Boolean(next.app.agent_updates_locked);
  await refreshConversationEvents(appId, refreshSequence);
  if (appId !== selectedAppId || selectedRefreshSequence !== refreshSequence) return;
  const knownApp = pendingApp || snapshot.app;
  if (compareAppVersions(next.app, knownApp) < 0) next.app = knownApp;
  const currentApp = snapshot.app;
  snapshot = { ...next, app: currentApp };
  if (!currentApp || (!hasAppBundle(currentApp) && hasAppBundle(next.app))) {
    // Opening an existing app and completing a first build should never
    // require a refresh click. Only replace an app the human can already use.
    applyAppVersion(next.app);
  } else if (compareAppVersions(next.app, currentApp) > 0) {
    // A state poll can observe a generated-App mutation after the transaction
    // commits but before its response reaches this frame. That response adopts
    // the revision and keeps the canvas interactive, so do not misclassify the
    // app's own write as a background agent update in the meantime.
    if (!appMutationInFlight(appId, currentApp.revision, next.app.revision)) {
      // Polling coalesces any number of background revisions into one latest
      // version. The current canvas remains stable until the human refreshes it.
      pendingApp = next.app;
      syncAppRefreshButton();
    }
  }
  renderChat();
  syncAgentSettings(snapshot.session);
  syncWorkspaceControls();
  armCapabilityWorker();
  markSelectedAppSeen();
}

// Connecting a provider from Home must reach an already-mounted panel without
// a page reload. The option matrix itself is static, so only a change in
// activation is re-rendered, leaving a model or effort mid-edit alone.
async function refreshRuntimeActivation(refreshSequence) {
  if (!Object.keys(sessionOptions).length) return;
  const options = await api("GET", "/session-options");
  if (refreshSequence !== appsRefreshSequence) return;
  const nextActive = Array.isArray(options.active_runtimes) ? options.active_runtimes : null;
  if (JSON.stringify(nextActive) === JSON.stringify(activeRuntimes)) return;
  activeRuntimes = nextActive;
  // An unconfigured app opens on a fallback nobody chose. If activation has
  // since made a different runtime usable, move onto it and rebuild the model
  // and effort with it, so connecting a provider makes an already-mounted app
  // runnable without reopening it. An established session is left alone: its
  // recorded configuration is a fact about what the app ran with.
  if (!establishedSession && !runtimeRunnable($("runtime").value)) {
    const opening = defaultSessionConfig();
    setRuntimeOptions(opening.agent_runtime || null);
    setSessionOptions(opening.model, opening.effort);
    return;
  }
  setRuntimeOptions($("runtime").value || null);
}

async function refresh() {
  const refreshSequence = ++appsRefreshSequence;
  try {
    await refreshRuntimeActivation(refreshSequence);
    const response = await api("GET", "/apps");
    if (refreshSequence !== appsRefreshSequence) return;
    apps = response.apps || [];
    const selected = apps.find(app => app.app_id === selectedAppId);
    if (selected) {
      selectedAppName = selected.name;
      selectedAgentUpdatesLocked = Boolean(selected.agent_updates_locked);
      selectedAppOutsideActiveIndex = false;
    } else if (selectedAppId && !selectedAppOutsideActiveIndex) {
      const removedAppId = selectedAppId;
      clearSelectedApp();
      if (window.location.hash === `#apps/${encodeURIComponent(removedAppId)}`) {
        window.KernHost.navigateWorkspace("apps", null, true);
      }
    }
    window.dispatchEvent(new CustomEvent("kern-web-apps-updated", {
      detail: { apps },
    }));
    syncWorkspaceControls();
    if (selectedAppId) {
      await refreshSelectedApp(selectedAppId);
      if (!$("recovery-drawer").hidden) {
        await refreshAdminPanel();
      }
    }
  } catch (_error) {
    if (refreshSequence === appsRefreshSequence) {
      showRuntimeStatus("Agentic Web App backend unavailable", "error");
    }
  }
}

function createApp() {
  if (!createAppPromise) {
    createAppPromise = createAppOnce().finally(() => { createAppPromise = null; });
  }
  return createAppPromise;
}

async function createAppOnce() {
  const selectionSequence = appSelectionSequence;
  try {
    const response = await api("POST", "/apps", {});
    await Promise.all([refresh(), window.KernHost.refreshNavigation()]);
    if (selectionSequence !== appSelectionSequence) return response.app;
    if (window.location.hash !== "#apps") return response.app;
    const app = apps.find(candidate => candidate.app_id === response.app.app_id) || response.app;
    await showApp(app);
    return app;
  } catch (error) {
    if (selectionSequence === appSelectionSequence) {
      showRuntimeStatus(error.message || "Could not create app", "error");
    }
  }
}

function setRenameAppOpen(open) {
  const overlay = $("rename-app-overlay");
  if (open) {
    if (!selectedAppId || selectedAppOutsideActiveIndex) return;
    renameAppReturnFocus = webAppsRoot.activeElement || $("rename-app");
    $("rename-app-input").value = selectedAppName || selectedAppId;
    $("rename-app-error").hidden = true;
    overlay.hidden = false;
    requestAnimationFrame(() => $("rename-app-input").select());
    return;
  }
  overlay.hidden = true;
  $("rename-app-save").disabled = false;
  if (renameAppReturnFocus && renameAppReturnFocus.isConnected) renameAppReturnFocus.focus();
  renameAppReturnFocus = null;
}

async function renameSelectedApp() {
  if (!selectedAppId || selectedAppOutsideActiveIndex) return;
  const appId = selectedAppId;
  const name = $("rename-app-input").value.trim();
  if (!name) {
    $("rename-app-error").textContent = "Enter an app name.";
    $("rename-app-error").hidden = false;
    $("rename-app-input").focus();
    return;
  }
  $("rename-app-save").disabled = true;
  const response = await api(
    "PUT",
    `/apps/${encodeURIComponent(appId)}/name`,
    { name },
  );
  apps = apps.map(app => app.app_id === appId ? { ...app, name: response.app.name } : app);
  if (selectedAppId === appId) selectedAppName = response.app.name;
  syncWorkspaceControls();
  setRenameAppOpen(false);
  await window.KernHost.refreshNavigation();
}

async function toggleAgentUpdateLock() {
  if (!selectedAppId || selectedAppOutsideActiveIndex || agentUpdateLockBusy) return;
  const appId = selectedAppId;
  const locked = !selectedAgentUpdatesLocked;
  agentUpdateLockBusy = true;
  syncWorkspaceControls();
  try {
    const response = await api(
      "PUT",
      `/apps/${encodeURIComponent(appId)}/agent-updates`,
      { locked },
    );
    apps = apps.map(app => app.app_id === appId ? {
      ...app,
      agent_updates_locked: Boolean(response.app.agent_updates_locked),
    } : app);
    if (selectedAppId !== appId) return;
    selectedAgentUpdatesLocked = Boolean(response.app.agent_updates_locked);
    showRuntimeStatus(
      selectedAgentUpdatesLocked
        ? "Agent updates locked. Agents will be asked to retry later."
        : "Agent updates unlocked.",
      "success",
    );
  } finally {
    agentUpdateLockBusy = false;
    if (selectedAppId === appId) syncWorkspaceControls();
  }
}

async function archiveSelectedApp() {
  if (!selectedAppId || selectedAppOutsideActiveIndex) return;
  const appId = selectedAppId;
  if (snapshot.status === "running") {
    showRuntimeStatus("Stop the agent before archiving this app", "error");
    return;
  }
  if (!confirm(`Archive ${selectedAppName || appId}?`)) return;
  const operationRoute = window.location.hash;
  await api("POST", `/apps/${encodeURIComponent(appId)}/archive`, {});
  if (selectedAppId === appId && window.location.hash === operationRoute) {
    clearSelectedApp();
    window.KernHost.navigateWorkspace("apps");
  }
  await Promise.all([refresh(), window.KernHost.refreshNavigation()]);
}

async function initialize() {
  generatedRoot = $("generated-host").attachShadow({ mode: "open" });
  generatedRoot.addEventListener("click", generatedInteraction);
  generatedRoot.addEventListener("change", generatedInteraction);
  generatedRoot.addEventListener("keydown", generatedEnterInteraction);
  generatedRoot.addEventListener("dragstart", generatedDragStart);
  generatedRoot.addEventListener("dragover", generatedDragOver);
  generatedRoot.addEventListener("dragleave", generatedDragLeave);
  generatedRoot.addEventListener("drop", generatedDrop);
  generatedRoot.addEventListener("dragend", clearGeneratedDrag);
  generatedRoot.addEventListener("submit", event => event.preventDefault());
  try {
    const options = await api("GET", "/session-options");
    sessionOptions = options.session_options || {};
    activeRuntimes = Array.isArray(options.active_runtimes) ? options.active_runtimes : null;
    setRuntimeOptions();
    setSessionOptions();
  } catch (_error) {
    showRuntimeStatus("Agent settings are unavailable", "error");
  }
  clearSelectedApp();
  await refresh();
  setInterval(refresh, 3000);
}

webAppsRoot.addEventListener("click", event => {
  const closest = selector => event.target.closest && event.target.closest(selector);
  const linkButton = closest(".md-copy-link");
  if (linkButton) {
    requestHostCopy(linkButton.dataset.copyHref || "").then(() => {
      linkButton.textContent = "Copied";
      setTimeout(() => { linkButton.textContent = linkButton.dataset.copyHref || ""; }, 1200);
    }).catch(error => showChatStatus(error.message, true));
    return;
  }
  const copyButton = closest(".md-copy");
  if (copyButton) {
    const code = copyButton.closest(".md-code")?.querySelector("code")?.textContent || "";
    requestHostCopy(code).then(() => {
      copyButton.textContent = "Copied";
      setTimeout(() => { copyButton.textContent = "Copy"; }, 1200);
    }).catch(error => showChatStatus(error.message, true));
    return;
  }
  const removeAttachmentButton = closest("button[data-remove-attachment]");
  if (removeAttachmentButton) {
    removeAttachment(removeAttachmentButton.dataset.removeAttachment)
      .catch(error => showChatStatus(error.message, true));
    return;
  }
  const restoreButton = closest("button[data-restore-revision]");
  if (restoreButton) {
    void restoreRevision(Number(restoreButton.dataset.restoreRevision));
    return;
  }
});
$("rename-app").addEventListener("click", () => setRenameAppOpen(true));
$("rename-app-close").addEventListener("click", () => setRenameAppOpen(false));
$("rename-app-cancel").addEventListener("click", () => setRenameAppOpen(false));
$("rename-app-backdrop").addEventListener("click", () => setRenameAppOpen(false));
$("rename-app-form").addEventListener("submit", event => {
  event.preventDefault();
  renameSelectedApp().catch(error => {
    $("rename-app-save").disabled = false;
    $("rename-app-error").textContent = error.message;
    $("rename-app-error").hidden = false;
  });
});
$("lock-agent-updates").addEventListener("click", () => toggleAgentUpdateLock().catch(error => showRuntimeStatus(error.message, "error")));
$("history-toggle").addEventListener("click", () => setHistoryMode(!historyMode));
$("chat-history-more").addEventListener("click", () => {
  void loadOlderConversationEvents();
});
$("chat-history-scroll").addEventListener("scroll", () => {
  if ($("chat-history-scroll").scrollTop <= 80) {
    void loadOlderConversationEvents();
  }
});
$("archive-app").addEventListener("click", () => archiveSelectedApp().catch(error => showRuntimeStatus(error.message, "error")));
$("app-refresh").addEventListener("click", applyPendingAppVersion);
$("settings-open").addEventListener("click", () => {
  setSettingsOpen($("settings-popover").hidden);
});
document.addEventListener("click", event => {
  if ($("settings-popover").hidden) return;
  const path = event.composedPath();
  if (path.includes($("settings-open")) || path.includes($("settings-popover"))) return;
  setSettingsOpen(false);
});
$("recovery-open").addEventListener("click", () => setRecoveryOpen($("recovery-drawer").hidden));
$("recovery-close").addEventListener("click", () => setRecoveryOpen(false));
$("recovery-backdrop").addEventListener("click", () => setRecoveryOpen(false));
$("latest-agent-dismiss").addEventListener("click", () => {
  if (transientAgentStatus) {
    transientAgentStatus = null;
    renderChat();
    return;
  }
  sessionAgentMessageApps.delete(selectedAppId);
  setDismissedAgentMessage(selectedAppId, conversationEntries()
    .filter(entry => ["agent", "error", "stopped"].includes(entry.kind))
    .at(-1)?.key || null);
  $("latest-agent-card").hidden = true;
});
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
webAppsRoot.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (!$("rename-app-overlay").hidden) {
    event.preventDefault();
    setRenameAppOpen(false);
    return;
  }
  if (!$("settings-popover").hidden || !$("recovery-drawer").hidden) {
    event.preventDefault();
    closeAdmin();
  }
});
const initializationPromise = initialize();

function afterInitialization(action, ...args) {
  return initializationPromise.then(() => action(...args));
}

window.KernWebApps = {
  clear: (...args) => afterInitialization(clearSelectedApp, ...args),
  create: (...args) => afterInitialization(createApp, ...args),
  open: (...args) => afterInitialization(showApp, ...args),
  refresh: (...args) => afterInitialization(refresh, ...args),
};
})();
