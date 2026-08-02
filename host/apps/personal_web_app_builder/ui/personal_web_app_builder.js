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
// The trusted block the backend injects after the provenance line of every
// outgoing message. Stripped from displayed user bubbles.
const CONTEXT_OPEN = "[Workspace context]";
const CONTEXT_CLOSE = "[/Workspace context]";
const textEncoder = new TextEncoder();
const textDecoder = new TextDecoder();
const pendingApi = new Map();
let requestCounter = 0;
let sessionOptions = {};
let apps = [];
let selectedAppId = null;
let selectedAppName = null;
let renderedAppsKey = "";
let workspacePanelOpen = true;
let snapshot = { app: null, session: null, status: "idle" };
let conversationEvents = [];
let conversationEventPages = freshConversationEventPages();
let loadingOlderConversationEvents = false;
let showingActivity = true;
let lastChatScrollTop = 0;
let restoredChatScrollTop = null;
const conversationViewStates = new Map();
const renderedChatEntries = new Map();
let renderedUiRevision = -1;
let renderedDataVersion = -1;
let generatedRoot = null;
let workerRun = null;
let armedWorker = null;
let bundleUrl = null;
let lastRenderKey = "";
let cssCacheKey = null;
let cssCacheValue = "";
let generatedDrag = null;
let appsRefreshSequence = 0;
const messageBusyApps = new Set();
let selectedRefreshSequence = 0;
let establishedSession = null;
let establishedSessionKey = "";
let pendingAttachments = [];
const attachmentActivities = new Map();
let adminOpen = false;
let adminTab = "chat";
let adminAutoOpened = false;
let adminReturnFocus = null;
let panelRefreshSequence = 0;
let recoveryPoints = [];
let instructionsLoadedFor = null;
let savedInstructionsMd = "";
let memoriesIndex = [];
let expandedMemory = null;
let scheduleRequestPending = false;
let runtimeStatusSequence = 0;

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
  if (clean.hasAttribute("data-drag-value")) clean.draggable = true;
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
  return Array.from(sheet.cssRules, sanitizeRule).filter(Boolean).join("\n");
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
    const styleText = `:host{display:block;min-height:100%;color:var(--text);background:var(--bg);font-family:system-ui,sans-serif}${safeCss}`;
    let style = generatedRoot.firstChild;
    if (!style || style.tagName !== "STYLE") {
      generatedRoot.replaceChildren();
      style = document.createElement("style");
      style.textContent = styleText;
      generatedRoot.append(style, fragment);
    } else {
      if (style.textContent !== styleText) style.textContent = styleText;
      patchSiblings(generatedRoot, style, fragment);
    }
    lastRenderKey = renderKey;
  }
  host.hidden = false;
  $("canvas-empty").hidden = true;
}

function patchSiblings(parent, afterNode, fragment) {
  const desired = Array.from(fragment.childNodes);
  let current = afterNode.nextSibling;
  for (const want of desired) {
    if (!current) {
      parent.append(want);
      continue;
    }
    current = patchNode(current, want).nextSibling;
  }
  while (current) {
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
  lastRenderKey = "";
  $("generated-host").hidden = true;
  $("canvas-empty").hidden = false;
}

function syncCanvasState() {
  if (!selectedAppId) return;
  const firstRun = !snapshot.session;
  $("empty-title").textContent = firstRun ? "Build this app" : "Your app will appear here";
  $("empty-description").textContent = firstRun
    ? "Open the agent chat and describe what you want. The agent creates the interface, behavior, and structured data."
    : "Open the agent chat to continue building.";
  $("empty-primary").textContent = "Open agent chat";
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

function generatedInteraction(event) {
  if (!selectedAppId) return;
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
  if (!selectedAppId || !(event.target instanceof Element)) return;
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
  if (!generatedDrag?.over) return;
  if (!(event.relatedTarget instanceof Node) || !generatedDrag.over.contains(event.relatedTarget)) {
    setGeneratedDropTarget(null);
  }
}

function generatedDrop(event) {
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
// path: the bundle's blob URL is cached per UI revision (so the engine can
// reuse its compiled script), and after each completed turn the next worker
// is spawned and initialized ahead of time ("armed"), so a user event starts
// its handler immediately instead of paying spawn + parse + init round trips.

function workerUrlFor(threadId, uiRevision) {
  if (bundleUrl && bundleUrl.threadId === threadId && bundleUrl.uiRevision === uiRevision) {
    return bundleUrl.url;
  }
  revokeBundleUrl();
  const source = (
    `(${capabilityWorkerBootstrap.toString()})(${MAX_RENDER_HTML_BYTES},${MAX_RENDER_CSS_BYTES});\n`
    + `${snapshot.app.javascript}\n`
  );
  const url = URL.createObjectURL(new Blob([source], { type: "application/javascript" }));
  bundleUrl = { threadId, uiRevision, url };
  return url;
}

function revokeBundleUrl() {
  if (!bundleUrl) return;
  URL.revokeObjectURL(bundleUrl.url);
  bundleUrl = null;
}

function discardArmedWorker() {
  if (!armedWorker) return;
  clearTimeout(armedWorker.timer);
  armedWorker.worker.terminate();
  armedWorker = null;
}

function armCapabilityWorker() {
  if (
    !selectedAppId || workerRun
    || !snapshot.app || !snapshot.app.javascript
  ) return;
  const app = snapshot.app;
  if (
    armedWorker
    && armedWorker.threadId === selectedAppId
    && armedWorker.uiRevision === app.ui_revision
    && armedWorker.dataVersion === app.data_version
  ) return;
  discardArmedWorker();
  const worker = new Worker(workerUrlFor(selectedAppId, app.ui_revision));
  const armed = {
    worker,
    threadId: selectedAppId,
    uiRevision: app.ui_revision,
    dataVersion: app.data_version,
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
  armed.timer = setTimeout(discard, WORKER_TURN_TIMEOUT_MS);
  worker.addEventListener("error", event => {
    event.preventDefault();
    if (armed.run) armed.run.finish("error");
    else discard();
  });
  worker.addEventListener("message", event => {
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
  if (!selectedAppId || !snapshot.app || !snapshot.app.javascript) return;
  if (workerRun) workerRun.finish("restarted");
  const threadId = selectedAppId;
  const app = snapshot.app;
  let armed = null;
  if (
    pendingEvent
    && armedWorker
    && armedWorker.state === "armed"
    && armedWorker.threadId === threadId
    && armedWorker.uiRevision === app.ui_revision
    && armedWorker.dataVersion === app.data_version
  ) {
    armed = armedWorker;
    armedWorker = null;
  } else {
    discardArmedWorker();
  }
  const worker = armed ? armed.worker : new Worker(workerUrlFor(threadId, app.ui_revision));
  const run = {
    worker,
    threadId,
    data: app.data,
    dataVersion: app.data_version,
    state: armed ? "event" : "starting",
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
      if (reason === "complete") setTimeout(armCapabilityWorker, 0);
    },
  };
  workerRun = run;
  run.timer = setTimeout(() => run.finish("timeout"), WORKER_TURN_TIMEOUT_MS);
  if (armed) {
    clearTimeout(armed.timer);
    armed.run = run;
    worker.postMessage({
      type: "event", action: pendingEvent.action, event: pendingEvent.event, turn_id: "turn",
    });
    return;
  }
  worker.addEventListener("error", event => {
    event.preventDefault();
    run.finish("error");
  });
  worker.addEventListener("message", event => handleWorkerMessage(run, event.data));
}

async function handleWorkerMessage(run, message) {
  if (
    workerRun !== run || selectedAppId !== run.threadId
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
    expected_data_version: run.dataVersion,
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
    snapshot.app = {
      ...snapshot.app,
      data: response.app.data,
      data_version: response.app.data_version,
      updated_at: response.app.updated_at,
    };
    run.data = response.app.data;
    run.dataVersion = response.app.data_version;
    renderedDataVersion = response.app.data_version;
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
  const sequence = ++runtimeStatusSequence;
  status.textContent = message;
  status.className = `runtime-status ${level}`;
  status.hidden = false;
  $("builder-shell").classList.add("runtime-status-visible");
  setTimeout(() => {
    if (runtimeStatusSequence !== sequence) return;
    status.hidden = true;
    $("builder-shell").classList.remove("runtime-status-visible");
  }, 4500);
}

// --- Admin overlay -----------------------------------------------------------

function openAdmin(tab = adminTab) {
  if (!selectedAppId) return;
  const wasOpen = adminOpen;
  if (!wasOpen && document.activeElement instanceof HTMLElement) {
    adminReturnFocus = document.activeElement;
  }
  adminOpen = true;
  $("builder-shell").classList.add("admin-active");
  $("admin-overlay").hidden = false;
  $("app-view").inert = true;
  $("admin-open").setAttribute("aria-expanded", "true");
  setAdminTab(tab);
  if (!wasOpen) requestAnimationFrame(() => $("admin-close").focus());
}

function closeAdmin() {
  const wasOpen = adminOpen;
  adminOpen = false;
  $("builder-shell").classList.remove("admin-active");
  $("admin-overlay").hidden = true;
  $("app-view").inert = false;
  $("admin-open").setAttribute("aria-expanded", "false");
  if (wasOpen) {
    const target = adminReturnFocus && adminReturnFocus.isConnected
      ? adminReturnFocus
      : $("admin-open");
    requestAnimationFrame(() => target.focus());
  }
  adminReturnFocus = null;
}

function setAdminTab(tab) {
  adminTab = tab;
  for (const button of document.querySelectorAll(".admin-tab")) {
    const active = button.dataset.adminTab === tab;
    button.classList.toggle("active", active);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  }
  $("panel-chat").hidden = tab !== "chat";
  $("panel-schedules").hidden = tab !== "schedules";
  $("panel-memory").hidden = tab !== "memory";
  $("panel-history").hidden = tab !== "history";
  updateActivityToggle();
  // A freshly selected workspace has not loaded its instruction value yet.
  // Keep the editor inert until that read completes so a fast first edit
  // cannot be overwritten by the asynchronous panel load.
  if (tab === "memory" && instructionsLoadedFor !== selectedAppId) {
    $("instructions-editor").disabled = true;
  }
  void refreshAdminPanel(true);
}

function updateActivityToggle() {
  const button = $("activity-toggle");
  button.hidden = !selectedAppId || adminTab !== "chat";
  button.setAttribute("aria-checked", showingActivity ? "true" : "false");
  button.title = showingActivity ? "Hide agent activity" : "Show agent activity";
  $("panel-chat").classList.toggle("activity-hidden", !showingActivity);
}

function toggleActivity() {
  const history = $("chat-history");
  const historyTop = history.getBoundingClientRect().top;
  const distanceFromBottom = history.scrollHeight - history.scrollTop - history.clientHeight;
  const anchor = Array.from($("chat-turns").querySelectorAll(".chat-entry:not(.chat-activity)"))
    .find(entry => entry.getBoundingClientRect().bottom > historyTop);
  const anchorOffset = anchor ? anchor.getBoundingClientRect().top - historyTop : null;
  showingActivity = !showingActivity;
  updateActivityToggle();
  if (distanceFromBottom < 48) {
    history.scrollTop = history.scrollHeight;
  } else if (anchor && anchorOffset !== null && anchor.isConnected) {
    history.scrollTop += anchor.getBoundingClientRect().top - historyTop - anchorOffset;
  }
  if (selectedAppId) {
    const threadId = selectedAppId;
    const refreshSequence = selectedRefreshSequence;
    void refreshConversationEvents(threadId, refreshSequence)
      .then(() => {
        if (selectedAppId === threadId && selectedRefreshSequence === refreshSequence) renderChat();
      })
      .catch(error => showChatStatus(error.message || "Could not load activity", true));
  }
}

async function refreshAdminPanel(opened = false) {
  if (!adminOpen || !selectedAppId) return;
  const sequence = ++panelRefreshSequence;
  const threadId = selectedAppId;
  const tab = adminTab;
  const panel = $(`panel-${tab}`);
  panel?.setAttribute("aria-busy", "true");
  if (opened) showPanelLoading(tab);
  try {
    if (adminTab === "schedules") {
      const response = await api("GET", `/apps/${encodeURIComponent(threadId)}/schedules`);
      if (sequence !== panelRefreshSequence || threadId !== selectedAppId) return;
      renderSchedules(response.schedules || []);
    } else if (adminTab === "history") {
      const response = await api("GET", `/apps/${encodeURIComponent(threadId)}/checkpoints`);
      if (sequence !== panelRefreshSequence || threadId !== selectedAppId) return;
      recoveryPoints = response.checkpoints || [];
      renderRecovery();
    } else if (adminTab === "memory" && opened) {
      // Never reload the memory panel on a poll: it would clobber edits.
      const [instructions, memories] = await Promise.all([
        api("GET", `/apps/${encodeURIComponent(threadId)}/instructions`),
        api("GET", `/apps/${encodeURIComponent(threadId)}/memories`),
      ]);
      if (sequence !== panelRefreshSequence || threadId !== selectedAppId) return;
      const instructionsDirty = instructionsLoadedFor === threadId
        && $("instructions-editor").value !== savedInstructionsMd;
      if (!instructionsDirty) renderInstructions(instructions);
      const inlineEditDirty = Boolean(expandedMemory?.editing && hasUnsavedAdminEdits());
      if (!inlineEditDirty) renderMemories(memories.memories || []);
    }
  } catch (error) {
    if (opened) showRuntimeStatus(error.message || "Could not load this panel", "error");
  } finally {
    if (sequence === panelRefreshSequence && threadId === selectedAppId) {
      panel?.removeAttribute("aria-busy");
    }
  }
}

function showPanelLoading(tab) {
  const targets = {
    schedules: ["schedules-list", "Loading schedules…"],
    memory: ["memories-list", "Loading saved topics…"],
    history: ["app-history-list", "Loading recovery points…"],
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

// --- Schedules panel ---------------------------------------------------------

function cadenceLabel(schedule) {
  if (schedule.cadence === "daily") return `Daily at ${schedule.daily_time} UTC`;
  const minutes = schedule.interval_minutes;
  if (minutes % 1440 === 0) return `Every ${minutes / 1440}d`;
  if (minutes % 60 === 0) return `Every ${minutes / 60}h`;
  return `Every ${minutes}m`;
}

function setSchedulePending(pending) {
  scheduleRequestPending = pending;
  $("schedule-create").disabled = pending;
  document.querySelectorAll("[data-schedule-toggle], [data-schedule-delete]")
    .forEach(button => { button.disabled = pending; });
}

function renderSchedules(schedules) {
  const list = $("schedules-list");
  list.replaceChildren();
  if (!schedules.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = "No schedules yet. The agent can also create them when you ask for recurring work.";
    list.append(empty);
    return;
  }
  for (const schedule of schedules) {
    const card = document.createElement("article");
    card.className = `schedule-card${schedule.enabled ? "" : " disabled"}`;

    const head = document.createElement("div");
    head.className = "schedule-head";
    const name = document.createElement("strong");
    name.textContent = schedule.name;
    const cadence = document.createElement("span");
    cadence.className = "schedule-cadence";
    cadence.textContent = cadenceLabel(schedule);
    head.append(name, cadence);
    if (schedule.created_by === "agent") {
      const chip = document.createElement("span");
      chip.className = "actor-chip agent";
      chip.textContent = "agent";
      head.append(chip);
    }

    const message = document.createElement("p");
    message.className = "schedule-message";
    message.textContent = schedule.message;

    const meta = document.createElement("div");
    meta.className = "schedule-meta";
    meta.textContent = schedule.enabled
      ? `Next ${relativeFuture(schedule.next_run_at)}${schedule.last_run_at ? ` · last ${relativeTime(schedule.last_run_at)}` : ""}`
      : "Paused";

    const actions = document.createElement("div");
    actions.className = "schedule-actions";
    const toggle = document.createElement("button");
    toggle.className = "ghost sm";
    toggle.dataset.scheduleToggle = String(schedule.id);
    toggle.dataset.scheduleEnabled = String(schedule.enabled);
    toggle.textContent = schedule.enabled ? "Pause" : "Resume";
    const remove = document.createElement("button");
    remove.className = "danger ghost sm";
    remove.dataset.scheduleDelete = String(schedule.id);
    remove.textContent = "Delete";
    toggle.disabled = remove.disabled = scheduleRequestPending;
    actions.append(toggle, remove);

    card.append(head, message, meta, actions);
    list.append(card);
  }
}

async function submitSchedule(event) {
  event.preventDefault();
  if (!selectedAppId || scheduleRequestPending) return;
  const threadId = selectedAppId;
  const cadence = $("schedule-cadence").value;
  const body = {
    name: $("schedule-name").value.trim(),
    message: $("schedule-message").value.trim(),
    cadence,
  };
  if (cadence === "interval") body.interval_minutes = Number($("schedule-interval").value);
  else body.daily_time = $("schedule-time").value;
  if (!body.name || !body.message) return;
  const button = $("schedule-create");
  setSchedulePending(true);
  button.textContent = "Adding…";
  try {
    await api("POST", `/apps/${encodeURIComponent(threadId)}/schedules`, body);
    if (selectedAppId !== threadId) return;
    $("schedule-name").value = "";
    $("schedule-message").value = "";
    await refreshAdminPanel(true);
    showRuntimeStatus("Schedule added", "success");
  } catch (error) {
    showRuntimeStatus(error.message || "Could not add the schedule", "error");
  } finally {
    setSchedulePending(false);
    button.textContent = "Add schedule";
  }
}

async function toggleSchedule(scheduleId, enabled) {
  if (!selectedAppId || scheduleRequestPending) return;
  const threadId = selectedAppId;
  setSchedulePending(true);
  try {
    await api(
      "PUT",
      `/apps/${encodeURIComponent(threadId)}/schedules/${scheduleId}`,
      { enabled: !enabled },
    );
    if (selectedAppId === threadId) await refreshAdminPanel(true);
  } finally {
    setSchedulePending(false);
  }
}

async function deleteSchedule(scheduleId) {
  if (
    !selectedAppId
    || scheduleRequestPending
    || !confirm("Delete this schedule?")
  ) return;
  const threadId = selectedAppId;
  setSchedulePending(true);
  try {
    await api("DELETE", `/apps/${encodeURIComponent(threadId)}/schedules/${scheduleId}`);
    if (selectedAppId === threadId) await refreshAdminPanel(true);
  } finally {
    setSchedulePending(false);
  }
}

// --- Memory panel ------------------------------------------------------------
// One panel holds everything the agent remembers: the always-on block at the
// top (injected into every turn) and the memory topic index below it. Topics
// read in place — tap to expand the body — and edit in place.

function renderInstructions(instructions) {
  instructionsLoadedFor = selectedAppId;
  savedInstructionsMd = instructions.instructions_md || "";
  $("instructions-editor").value = savedInstructionsMd;
  $("instructions-editor").disabled = false;
  $("instructions-status").textContent = "";
  $("instructions-meta").textContent = instructions.updated_by
    ? `Last edited by ${instructions.updated_by === "user" ? "you" : "the agent"} · ${relativeTime(instructions.updated_at)}`
    : "";
  syncInstructionsDirtyState();
}

function syncInstructionsDirtyState() {
  const dirty = instructionsLoadedFor === selectedAppId
    && $("instructions-editor").value !== savedInstructionsMd;
  $("instructions-actions").hidden = !dirty && !$("instructions-status").textContent;
  $("instructions-discard").hidden = !dirty;
  $("instructions-save").disabled = !dirty;
}

async function saveInstructions() {
  if (!selectedAppId || instructionsLoadedFor !== selectedAppId) return;
  const threadId = selectedAppId;
  try {
    const response = await api(
      "PUT",
      `/apps/${encodeURIComponent(threadId)}/instructions`,
      { instructions_md: $("instructions-editor").value },
    );
    if (selectedAppId !== threadId) return;
    renderInstructions(response);
    $("instructions-status").textContent = "Saved";
    $("instructions-actions").hidden = false;
  } catch (error) {
    $("instructions-status").textContent = error.message || "Could not save";
    $("instructions-actions").hidden = false;
  }
}

function discardInstructions() {
  if (instructionsLoadedFor !== selectedAppId) return;
  $("instructions-editor").value = savedInstructionsMd;
  $("instructions-status").textContent = "";
  syncInstructionsDirtyState();
}

function renderMemories(memories) {
  memoriesIndex = memories;
  const list = $("memories-list");
  list.replaceChildren();
  if (!memories.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = "No memories yet. The agent saves durable topics here; you can add or edit them too.";
    list.append(empty);
    return;
  }
  for (const memory of memories) {
    const expanded = expandedMemory && expandedMemory.name === memory.name;
    const item = document.createElement("div");
    item.className = `memory-item${expanded ? " expanded" : ""}`;

    const head = document.createElement("button");
    head.className = "memory-item-head";
    head.dataset.memoryToggle = memory.name;
    head.setAttribute("aria-expanded", String(Boolean(expanded)));
    const title = document.createElement("span");
    title.className = "memory-item-title";
    const name = document.createElement("strong");
    name.textContent = memory.name;
    const description = document.createElement("span");
    description.className = "memory-description";
    description.textContent = memory.description;
    title.append(name, description);
    const meta = document.createElement("span");
    meta.className = "memory-meta";
    meta.textContent = `${memory.updated_by === "user" ? "you" : "agent"} · ${relativeTime(memory.updated_at)}`;
    const chevron = document.createElement("span");
    chevron.className = "memory-chevron";
    chevron.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="m7 8 3 3 3-3" fill="none" stroke="currentColor" stroke-width="1.7" stroke-linecap="round" stroke-linejoin="round"/></svg>';
    head.append(title, meta, chevron);
    item.append(head);

    if (expanded) item.append(renderExpandedMemory());
    list.append(item);
  }
}

function renderExpandedMemory() {
  const detail = document.createElement("div");
  detail.className = "memory-detail";
  const memory = expandedMemory.memory;
  if (!memory) {
    detail.textContent = "Loading…";
    return detail;
  }
  if (expandedMemory.editing) {
    const description = document.createElement("input");
    description.id = "memory-edit-description";
    description.maxLength = 150;
    description.value = memory.description;
    const descriptionField = document.createElement("label");
    descriptionField.className = "field";
    descriptionField.innerHTML = "<span>One-line description</span>";
    descriptionField.append(description);
    const body = document.createElement("textarea");
    body.id = "memory-edit-body";
    body.rows = 8;
    body.value = memory.body_md;
    const bodyField = document.createElement("label");
    bodyField.className = "field";
    bodyField.innerHTML = "<span>Body (markdown)</span>";
    bodyField.append(body);
    const actions = document.createElement("div");
    actions.className = "memory-card-actions";
    const cancel = document.createElement("button");
    cancel.className = "ghost sm";
    cancel.dataset.memoryEditCancel = memory.name;
    cancel.textContent = "Cancel";
    const save = document.createElement("button");
    save.className = "primary sm";
    save.dataset.memoryEditSave = memory.name;
    save.textContent = "Save";
    const spacer = document.createElement("span");
    spacer.className = "memory-actions-spacer";
    actions.append(spacer, cancel, save);
    detail.append(descriptionField, bodyField, actions);
    return detail;
  }
  const body = document.createElement("pre");
  body.className = "memory-body-view";
  body.textContent = memory.body_md || "(empty)";
  detail.append(body);
  const actions = document.createElement("div");
  actions.className = "memory-card-actions";
  const remove = document.createElement("button");
  remove.className = "danger ghost sm";
  remove.dataset.memoryDeleteName = memory.name;
  remove.textContent = "Delete";
  const spacer = document.createElement("span");
  spacer.className = "memory-actions-spacer";
  const edit = document.createElement("button");
  edit.className = "ghost sm";
  edit.dataset.memoryEdit = memory.name;
  edit.textContent = "Edit";
  actions.append(remove, spacer, edit);
  detail.append(actions);
  return detail;
}

function openMemoryEditor() {
  expandedMemory = null;
  $("memory-editor").hidden = false;
  $("memory-name").value = "";
  $("memory-description").value = "";
  $("memory-body").value = "";
  renderMemories(memoriesIndex);
  $("memory-name").focus();
}

function closeMemoryEditor() {
  expandedMemory = null;
  const editor = $("memory-editor");
  if (editor) editor.hidden = true;
}

async function toggleMemory(name, editing = false) {
  if (expandedMemory && expandedMemory.name === name && !editing) {
    expandedMemory = null;
    renderMemories(memoriesIndex);
    return;
  }
  const threadId = selectedAppId;
  expandedMemory = { name, memory: null, editing };
  renderMemories(memoriesIndex);
  try {
    const response = await api(
      "GET",
      `/apps/${encodeURIComponent(threadId)}/memories/${encodeURIComponent(name)}`,
    );
    if (selectedAppId !== threadId || !expandedMemory || expandedMemory.name !== name) return;
    expandedMemory.memory = response.memory;
    renderMemories(memoriesIndex);
  } catch (error) {
    expandedMemory = null;
    renderMemories(memoriesIndex);
    showRuntimeStatus(error.message || "Could not load the memory", "error");
  }
}

async function saveMemoryWith(name, description, bodyMd, isNew) {
  if (!selectedAppId) return;
  const threadId = selectedAppId;
  if (!name) {
    showRuntimeStatus("Memory needs a lowercase-slug name", "error");
    return;
  }
  try {
    const response = await api(
      "PUT",
      `/apps/${encodeURIComponent(threadId)}/memories/${encodeURIComponent(name)}`,
      { description, body_md: bodyMd },
    );
    if (selectedAppId !== threadId) return;
    if (isNew) $("memory-editor").hidden = true;
    // A new topic joins the index collapsed so its first tap consistently
    // means "read". An edited topic stays open to show the saved result.
    expandedMemory = isNew ? null : { name, memory: response.memory, editing: false };
    await refreshAdminPanel(true);
    showRuntimeStatus("Memory saved", "success");
  } catch (error) {
    showRuntimeStatus(error.message || "Could not save the memory", "error");
  }
}

async function deleteMemoryNamed(name) {
  if (!selectedAppId || !confirm(`Delete memory "${name}"?`)) return;
  try {
    await api(
      "DELETE",
      `/apps/${encodeURIComponent(selectedAppId)}/memories/${encodeURIComponent(name)}`,
    );
    expandedMemory = null;
    await refreshAdminPanel(true);
  } catch (error) {
    showRuntimeStatus(error.message || "Could not delete the memory", "error");
  }
}

// --- Recovery panel ----------------------------------------------------------

function historyIcon(kind) {
  if (kind === "checkpoint") {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6 4.5v11l4-2.5 4 2.5v-11z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  }
  if (kind === "ui") {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="4" y="4" width="12" height="12" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M4 8.5h12" stroke="currentColor" stroke-width="1.5"/></svg>';
  }
  if (kind === "snapshot") {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M6 4.5v11l4-2.5 4 2.5v-11z" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  }
  if (kind === "instructions" || kind === "memory") {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><path d="M5 4.5h8.5A1.5 1.5 0 0 1 15 6v9.5l-3-1.8-3 1.8V6H5v9.5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linejoin="round"/></svg>';
  }
  if (kind === "schedule") {
    return '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="6.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M10 6.5V10l2.5 2" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>';
  }
  return '<svg viewBox="0 0 20 20" aria-hidden="true"><circle cx="10" cy="10" r="3" fill="currentColor"/></svg>';
}

function renderRecovery() {
  const list = $("app-history-list");
  list.replaceChildren();
  if (!recoveryPoints.length) {
    const empty = document.createElement("p");
    empty.className = "panel-empty";
    empty.textContent = "No recovery points yet. Save a checkpoint now, or wait for the next daily snapshot.";
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
    resource.textContent = entry.checkpoint_type === "manual" ? "Manual" : "Daily";
    const summary = document.createElement("span");
    summary.className = "history-summary";
    summary.textContent = entry.summary;
    const meta = document.createElement("span");
    meta.className = "history-meta";
    meta.textContent = new Date(entry.created_at).toLocaleString();
    meta.title = `Interface revision ${entry.ui_revision}, data version ${entry.data_version}`;
    body.append(resource, summary, meta);

    item.append(icon, body);
    const revert = document.createElement("button");
    revert.className = "ghost sm history-revert";
    revert.dataset.revertId = String(entry.id);
    revert.textContent = "Revert";
    item.append(revert);
    list.append(item);
  });
}

async function revertCheckpoint(historyId) {
  if (!selectedAppId) return;
  const entry = recoveryPoints.find(candidate => candidate.id === historyId);
  if (!entry || !confirm(entry.revert_prompt || "Revert this change?")) return;
  const threadId = selectedAppId;
  try {
    await api(
      "POST",
      `/apps/${encodeURIComponent(threadId)}/checkpoints/${historyId}/revert`,
      {},
      AGENT_DELIVERY_TIMEOUT_MS,
    );
    if (selectedAppId !== threadId) return;
    showRuntimeStatus("Workspace restored", "success");
    closeMemoryEditor();
    instructionsLoadedFor = null;
    await refresh();
    await refreshAdminPanel(true);
  } catch (error) {
    showRuntimeStatus(error.message || "Could not revert this change", "error");
  }
}

async function saveCheckpoint() {
  if (!selectedAppId) return;
  const threadId = selectedAppId;
  const button = $("checkpoint-save");
  button.disabled = true;
  button.textContent = "Saving…";
  $("checkpoint-status").textContent = "";
  try {
    await api("POST", `/apps/${encodeURIComponent(threadId)}/checkpoints`, {});
    if (selectedAppId !== threadId) return;
    $("checkpoint-status").textContent = "Today’s saved checkpoint is up to date.";
    await refreshAdminPanel(true);
  } catch (error) {
    $("checkpoint-status").textContent = error.message || "Could not save checkpoint";
  } finally {
    button.disabled = false;
    button.textContent = "Save checkpoint";
  }
}

// --- Chat --------------------------------------------------------------------

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
  // Pointer activation already respects the disabled button. Match that
  // behavior for Enter and any other programmatic composer submission while
  // attachments or session options make the draft invalid.
  if (!fromGeneratedApp && $("send-message").disabled) return;
  const threadId = targetAppId || selectedAppId;
  const submittedDraft = fromGeneratedApp ? null : $("message").value;
  const message = (fromGeneratedApp ? forcedMessage : submittedDraft).trim();
  const attachments = fromGeneratedApp ? [] : pendingAttachments;
  if (
    (!message && !attachments.length)
    || !threadId
    || threadId !== selectedAppId
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
  if (!threadId || snapshot.status !== "running") return;
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
  const openActivities = new Set(
    Array.from(entries.querySelectorAll(".chat-activity-card[open]"))
      .map(card => card.closest(".chat-entry")?.dataset.entryId)
      .filter(Boolean),
  );
  renderHistoryLoader();
  if (!ordered.length) {
    renderedChatEntries.clear();
    entries.replaceChildren();
    const empty = document.createElement("p");
    empty.className = "chat-empty";
    empty.textContent = "Describe the app you want. The agent can build its UI, behavior, and data, then keep changing it here.";
    entries.append(empty);
    syncAgentSettings(snapshot.session);
    return;
  }
  if (entries.firstElementChild?.classList.contains("chat-empty")) entries.replaceChildren();
  ordered.forEach((entry, index) => {
    const renderedKey = JSON.stringify(entry);
    const current = entries.children[index];
    if (!current || current.dataset.entryId !== entry.key) {
      entries.insertBefore(renderChatEntry(entry, openActivities.has(entry.key)), current || null);
    } else if (renderedChatEntries.get(entry.key) !== renderedKey) {
      entries.replaceChild(renderChatEntry(entry, openActivities.has(entry.key)), current);
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

function displayedUserMessage(message) {
  // Hide the injected workspace-context block from user bubbles. It is the
  // block directly after the provenance line; nothing else is stripped.
  const lines = message.split("\n");
  if (lines.length > 1 && lines[1] === CONTEXT_OPEN) {
    const end = lines.indexOf(CONTEXT_CLOSE, 1);
    if (end !== -1) return [lines[0], ...lines.slice(end + 1)].join("\n");
  }
  return message;
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
    } else if (event.event_type === "thread.activity") {
      entries.push({
        key: `event-${event.seq}`,
        kind: "activity",
        activity: payload.activity && typeof payload.activity === "object"
          ? payload.activity
          : {},
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
        message: fromUser ? displayedUserMessage(payload.message) : payload.message,
      });
    }
  }
  return entries;
}

function renderActivityCard(activity, open) {
  const requestedKind = String(activity.kind || "status").replace(/[^a-z_]/g, "");
  const kind = Object.prototype.hasOwnProperty.call(ACTIVITY_PRESENTATION, requestedKind)
    ? requestedKind
    : "status";
  const presentation = ACTIVITY_PRESENTATION[kind];
  const detail = typeof activity.detail === "string" ? activity.detail : "";
  const output = typeof activity.output === "string" ? activity.output : "";
  const expandable = Boolean(detail || output);
  const card = document.createElement(expandable ? "details" : "div");
  card.className = `chat-activity-card activity-${kind}`;
  if (expandable && open) card.open = true;
  if (!expandable) card.classList.add("activity-static");
  const title = typeof activity.title === "string" && activity.title
    ? activity.title
    : "Agent activity";
  card.setAttribute("aria-label", `${presentation.label}: ${title}`);
  const summary = document.createElement(expandable ? "summary" : "div");
  summary.className = "chat-activity-summary";
  const icon = document.createElement("span");
  icon.className = "chat-activity-icon";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = presentation.icon;
  const heading = document.createElement("span");
  heading.className = "chat-activity-heading";
  const titleElement = document.createElement("span");
  titleElement.className = "chat-activity-title";
  titleElement.textContent = title;
  const kindElement = document.createElement("span");
  kindElement.className = "chat-activity-kind";
  kindElement.textContent = presentation.label;
  heading.append(titleElement, kindElement);
  summary.append(icon, heading);
  const rawStatus = typeof activity.status === "string" ? activity.status : "";
  if (rawStatus && !["completed", "running"].includes(rawStatus.toLowerCase())) {
    const status = document.createElement("span");
    status.className = "chat-activity-status";
    if (/(?:fail|error|denied|exit\s+[1-9])/i.test(rawStatus)) status.classList.add("failed");
    status.textContent = rawStatus;
    summary.append(status);
  }
  if (activity.phase === "started") {
    const phase = document.createElement("span");
    phase.className = "chat-activity-phase";
    phase.textContent = "Started";
    summary.append(phase);
    card.classList.add("started");
  }
  card.append(summary);
  if (expandable) {
    const body = document.createElement("div");
    body.className = "chat-activity-body";
    for (const [label, value] of [
      [presentation.detail, detail],
      [presentation.output, output],
    ]) {
      if (!value) continue;
      const section = document.createElement("section");
      const labelElement = document.createElement("div");
      labelElement.className = "chat-activity-label";
      labelElement.textContent = label;
      const pre = document.createElement("pre");
      pre.textContent = value;
      section.append(labelElement, pre);
      body.append(section);
    }
    card.append(body);
  }
  return card;
}

function renderChatEntry(entryData, activityOpen = false) {
  const entry = document.createElement("article");
  entry.className = `chat-entry chat-${entryData.kind}`;
  entry.dataset.entryId = entryData.key;
  if (entryData.kind === "activity") {
    entry.append(renderActivityCard(entryData.activity, activityOpen));
  } else if (entryData.kind === "agent") {
    entry.classList.add("md-content");
    entry.innerHTML = KernRichText.renderMarkdown(entryData.message);
  } else {
    entry.textContent = entryData.message;
  }
  return entry;
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
  return showingActivity
    ? conversationEventPages.all
    : conversationEventPages.conversation;
}

function conversationEventsPath(threadId, pageState, cursorName = null, cursor = null) {
  const query = [];
  if (pageState === conversationEventPages.conversation) query.push("activity=false");
  if (cursorName !== null) query.push(`${cursorName}=${cursor}`);
  const suffix = query.length ? `?${query.join("&")}` : "";
  return `/apps/${encodeURIComponent(threadId)}/conversation/events${suffix}`;
}

async function refreshConversationEvents(threadId, refreshSequence) {
  const pageState = activeConversationEventPage();
  if (!pageState.initialized) {
    const response = await api(
      "GET",
      conversationEventsPath(threadId, pageState),
    );
    if (selectedAppId !== threadId || selectedRefreshSequence !== refreshSequence) return;
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
        conversationEventsPath(threadId, pageState, "before", before),
      );
      if (selectedAppId !== threadId || selectedRefreshSequence !== refreshSequence) return;
      oldestPage = (olderResponse.events || []).filter(event => event.seq < before);
      if (oldestPage.length) {
        mergeConversationEvents(oldestPage);
        pageState.oldestSeq = oldestPage[0].seq;
      }
    }
    pageState.hasOlder = oldestPage.length === CONVERSATION_EVENTS_PAGE;
    pageState.initialized = true;
    renderHistoryLoader();
    return;
  }
  for (;;) {
    const since = pageState.newestSeq;
    const response = await api(
      "GET",
      conversationEventsPath(threadId, pageState, "since", since),
    );
    if (selectedAppId !== threadId || selectedRefreshSequence !== refreshSequence) return;
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

async function loadOlderConversationEvents() {
  const pageState = activeConversationEventPage();
  if (
    !selectedAppId
    || !pageState.initialized
    || !pageState.hasOlder
    || loadingOlderConversationEvents
    || pageState.oldestSeq === null
  ) return;
  const threadId = selectedAppId;
  const before = pageState.oldestSeq;
  loadingOlderConversationEvents = true;
  renderHistoryLoader();
  try {
    const response = await api(
      "GET",
      conversationEventsPath(threadId, pageState, "before", before),
    );
    if (
      selectedAppId !== threadId
      || activeConversationEventPage() !== pageState
      || pageState.oldestSeq !== before
    ) return;
    const older = (response.events || []).filter(event => event.seq < before);
    const history = $("chat-history");
    const previousHeight = history.scrollHeight;
    const previousTop = history.scrollTop;
    if (older.length) {
      mergeConversationEvents(older);
      pageState.oldestSeq = older[0].seq;
    }
    pageState.hasOlder = older.length === CONVERSATION_EVENTS_PAGE;
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
  const pageState = activeConversationEventPage();
  loader.hidden = !selectedAppId || !pageState.hasOlder;
  loader.dataset.oldestSeq = pageState.oldestSeq === null
    ? ""
    : String(pageState.oldestSeq);
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
  $("composer-running").hidden = !running;
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

function relativeFuture(value) {
  const timestamp = Date.parse(value || "");
  if (!Number.isFinite(timestamp)) return "soon";
  const seconds = Math.floor((timestamp - Date.now()) / 1000);
  if (seconds <= 60) return "in under a minute";
  if (seconds < 3600) return `in ${Math.round(seconds / 60)}m`;
  if (seconds < 86400) return `in ${Math.round(seconds / 3600)}h`;
  return `in ${Math.round(seconds / 86400)}d`;
}

// --- Home view ---------------------------------------------------------------

function renderApps() {
  const key = JSON.stringify([selectedAppId, apps]);
  if (key === renderedAppsKey) return;
  renderedAppsKey = key;
  const grid = $("apps");
  grid.replaceChildren();
  $("home-view").classList.toggle("empty-library", !apps.length);
  $("home-empty").hidden = Boolean(apps.length);
  $("home-empty-title").textContent = "Build your first app";
  $("home-empty-description").textContent = "Create a workspace, then tell its agent what you want. Interface, behavior, and data all grow from the conversation.";
  renderWorkspaceList();
  for (const app of apps) {
    const card = document.createElement("button");
    card.className = "app-card";
    card.dataset.appId = app.thread_id;

    const head = document.createElement("span");
    head.className = "app-card-head";
    const name = document.createElement("span");
    name.className = "app-card-name";
    name.textContent = app.name;
    head.append(name);
    if (app.status === "running") {
      const dot = document.createElement("span");
      dot.className = "app-dot";
      dot.title = "Agent is working";
      head.append(dot);
    }

    const session = document.createElement("span");
    session.className = "app-card-meta";
    session.textContent = app.session
      ? `${runtimeLabel(app.session.agent_runtime)} · ${app.session.model}`
      : "No agent session yet";

    const details = document.createElement("span");
    details.className = "app-card-meta dim";
    details.textContent = `v${app.ui_revision} · ${relativeTime(app.last_used_at)}`;

    card.append(head, session, details);
    grid.append(card);
  }
}

function renderWorkspaceList() {
  const list = $("workspace-list");
  list.replaceChildren();
  $("workspace-list-empty").hidden = Boolean(apps.length);
  for (const app of apps) {
    const button = document.createElement("button");
    button.className = "workspace-list-item";
    button.classList.toggle("current", app.thread_id === selectedAppId);
    button.dataset.workspaceId = app.thread_id;
    if (app.thread_id === selectedAppId) button.setAttribute("aria-current", "page");

    const icon = document.createElement("span");
    icon.className = "workspace-list-icon";
    icon.innerHTML = `<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="3.5" y="3.5" width="13" height="13" rx="2" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M7 7h6M7 10h6M7 13h3" fill="none" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/></svg>`;
    const name = document.createElement("span");
    name.className = "workspace-list-name";
    name.textContent = app.name;
    const meta = document.createElement("span");
    meta.className = "workspace-list-meta";
    meta.textContent = app.session
      ? `${runtimeLabel(app.session.agent_runtime)} · ${relativeTime(app.last_used_at)}`
      : `Not built · ${relativeTime(app.last_used_at)}`;
    button.append(icon, name, meta);
    if (app.status === "running") {
      const running = document.createElement("span");
      running.className = "workspace-list-running";
      running.title = "Agent is working";
      button.append(running);
    }
    list.append(button);
  }
}

function syncWorkspacePanel() {
  const mobile = matchMedia("(max-width: 720px)").matches;
  if (!mobile) workspacePanelOpen = true;
  $("builder-shell").classList.toggle("workspace-panel-open", workspacePanelOpen);
  $("workspace-panel").inert = !workspacePanelOpen;
  $("workspace-panel-backdrop").hidden = !workspacePanelOpen || !mobile;
  $("workspace-panel-open").hidden = workspacePanelOpen;
  $("workspace-panel-open").setAttribute("aria-expanded", String(workspacePanelOpen));
}

function setWorkspacePanelOpen(open, focusTarget = false) {
  workspacePanelOpen = Boolean(open);
  syncWorkspacePanel();
  if (!focusTarget) return;
  (workspacePanelOpen ? $("workspace-panel-close") : $("workspace-panel-open")).focus();
}

function syncWorkspaceControls() {
  const hasApp = selectedAppId !== null;
  $("admin-fab-label").textContent = "View admin";
  $("admin-app-title").textContent = selectedAppName || "";
  $("chat-composer").hidden = !hasApp;
  $("home-tagline").textContent = "Describe an app. The agent builds it, runs it, and keeps it alive.";
  $("checkpoint-save").disabled = !hasApp;
  $("schedule-create").disabled = !hasApp || scheduleRequestPending;
  if (!hasApp) $("revision-label").textContent = "";
  updateActivityToggle();
  setSessionOptions();
  syncCanvasState();
}

function stopCapabilityWorker() {
  if (workerRun) workerRun.finish("workspace-switched");
  discardArmedWorker();
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
    conversationEventPages = freshConversationEventPages();
    lastChatScrollTop = 0;
    restoredChatScrollTop = null;
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
    lastChatScrollTop = state.scrollTop;
    restoredChatScrollTop = state.scrollTop;
  }
  loadingOlderConversationEvents = false;
  renderHistoryLoader();
}

function hasUnsavedAdminEdits() {
  const instructionsDirty = instructionsLoadedFor === selectedAppId
    && $("instructions-editor").value !== savedInstructionsMd;
  const newMemoryDirty = !$("memory-editor").hidden && [
    $("memory-name").value,
    $("memory-description").value,
    $("memory-body").value,
  ].some(value => value.trim());
  const editDescription = $("memory-edit-description");
  const editBody = $("memory-edit-body");
  const memoryEditDirty = Boolean(
    expandedMemory?.editing
    && expandedMemory.memory
    && editDescription
    && editBody
    && (
      editDescription.value !== expandedMemory.memory.description
      || editBody.value !== expandedMemory.memory.body_md
    )
  );
  return instructionsDirty || newMemoryDirty || memoryEditDirty;
}

function confirmDiscardAdminEdits() {
  return !hasUnsavedAdminEdits() || confirm("Discard unsaved admin edits?");
}

function clearSelectedApp(force = false) {
  if (!force && !confirmDiscardAdminEdits()) return false;
  saveComposerDraft();
  saveSelectedConversationView();
  clearPendingAttachments();
  stopCapabilityWorker();
  revokeBundleUrl();
  closeMemoryEditor();
  selectedRefreshSequence += 1;
  panelRefreshSequence += 1;
  selectedAppId = null;
  selectedAppName = null;
  adminAutoOpened = false;
  instructionsLoadedFor = null;
  recoveryPoints = [];
  restoreComposerDraft();
  snapshot = { app: null, session: null, status: "idle" };
  conversationEvents = [];
  conversationEventPages = freshConversationEventPages();
  loadingOlderConversationEvents = false;
  lastChatScrollTop = 0;
  restoredChatScrollTop = null;
  renderedChatEntries.clear();
  renderedUiRevision = -1;
  renderedDataVersion = -1;
  establishedSession = null;
  establishedSessionKey = "";
  clearGenerated();
  closeAdmin();
  $("app-view").hidden = true;
  $("home-view").hidden = false;
  renderChat();
  renderApps();
  syncWorkspaceControls();
  return true;
}

async function showApp(app) {
  if (
    selectedAppId
    && selectedAppId !== app.thread_id
    && !confirmDiscardAdminEdits()
  ) return;
  saveComposerDraft();
  saveSelectedConversationView();
  if (selectedAppId !== app.thread_id) clearPendingAttachments();
  stopCapabilityWorker();
  revokeBundleUrl();
  closeMemoryEditor();
  selectedRefreshSequence += 1;
  panelRefreshSequence += 1;
  selectedAppId = app.thread_id;
  selectedAppName = app.name;
  adminAutoOpened = false;
  instructionsLoadedFor = null;
  recoveryPoints = [];
  restoreComposerDraft();
  restoreConversationView(app);
  renderedChatEntries.clear();
  renderedUiRevision = -1;
  renderedDataVersion = -1;
  establishedSession = null;
  establishedSessionKey = "";
  clearGenerated();
  closeAdmin();
  adminTab = "chat";
  $("home-view").hidden = true;
  $("app-view").hidden = false;
  if (matchMedia("(max-width: 720px)").matches) setWorkspacePanelOpen(false);
  renderChat();
  renderApps();
  syncWorkspaceControls();
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
  if (
    snapshot.app
    && (
      next.app.ui_revision < snapshot.app.ui_revision
      || (
        next.app.ui_revision === snapshot.app.ui_revision
        && next.app.data_version < snapshot.app.data_version
      )
    )
  ) {
    next.app = snapshot.app;
  }
  snapshot = next;
  const hasBundle = Boolean(next.app.html || next.app.css || next.app.javascript);
  if (next.app.ui_revision !== renderedUiRevision) {
    stopCapabilityWorker();
    renderedUiRevision = next.app.ui_revision;
    renderedDataVersion = next.app.data_version;
    if (hasBundle) {
      $("revision-label").textContent = `Revision ${next.app.ui_revision}`;
      renderGenerated(next.app.html, next.app.css);
      runCapabilityWorker();
    } else {
      $("revision-label").textContent = "Empty app";
      clearGenerated();
    }
  } else if (next.app.data_version !== renderedDataVersion) {
    // Data moved (usually an agent write) while the interface is unchanged:
    // one load turn re-renders from durable data without tearing the
    // canvas down. Keep the old marker while another worker is active so a
    // later poll cannot mistake an update that was never rendered for one
    // that completed.
    if (hasBundle && !workerRun) {
      renderedDataVersion = next.app.data_version;
      runCapabilityWorker();
    }
  }
  if (!hasBundle && !adminOpen && !adminAutoOpened) {
    // A fresh workspace is an empty canvas; open the admin chat so the
    // human lands where building starts.
    adminAutoOpened = true;
    openAdmin("chat");
  }
  renderChat();
  syncAgentSettings(snapshot.session);
  syncWorkspaceControls();
  armCapabilityWorker();
}

async function refresh() {
  const refreshSequence = ++appsRefreshSequence;
  try {
    const response = await api("GET", "/apps");
    if (refreshSequence !== appsRefreshSequence) return;
    apps = response.apps || [];
    const selected = apps.find(app => app.thread_id === selectedAppId);
    if (selected) {
      selectedAppName = selected.name;
    } else if (selectedAppId) {
      clearSelectedApp(true);
    }
    renderApps();
    syncWorkspaceControls();
    if (selectedAppId) {
      await refreshSelectedApp(selectedAppId);
      if (adminOpen && (adminTab === "schedules" || adminTab === "history")) {
        await refreshAdminPanel();
      }
    }
  } catch (_error) {
    if (refreshSequence === appsRefreshSequence) {
      showRuntimeStatus("Agentic Web App backend unavailable", "error");
    }
  }
}

async function createApp() {
  const buttons = [$("new-app"), $("home-empty-primary"), $("workspace-new-app")];
  buttons.forEach(button => { button.disabled = true; });
  try {
    const response = await api("POST", "/apps", {});
    await refresh();
    const app = apps.find(candidate => candidate.thread_id === response.app.thread_id) || response.app;
    await showApp(app);
  } catch (error) {
    showRuntimeStatus(error.message || "Could not create app", "error");
  } finally {
    buttons.forEach(button => { button.disabled = false; });
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

async function initialize() {
  generatedRoot = $("generated-host").attachShadow({ mode: "open" });
  generatedRoot.addEventListener("click", generatedInteraction);
  generatedRoot.addEventListener("change", generatedInteraction);
  generatedRoot.addEventListener("dragstart", generatedDragStart);
  generatedRoot.addEventListener("dragover", generatedDragOver);
  generatedRoot.addEventListener("dragleave", generatedDragLeave);
  generatedRoot.addEventListener("drop", generatedDrop);
  generatedRoot.addEventListener("dragend", clearGeneratedDrag);
  generatedRoot.addEventListener("submit", event => event.preventDefault());
  syncWorkspacePanel();
  try {
    const options = await api("GET", "/session-options");
    sessionOptions = options.session_options || {};
    setRuntimeOptions();
    setSessionOptions();
  } catch (_error) {
    showRuntimeStatus("Agent settings are unavailable", "error");
  }
  clearSelectedApp(true);
  await refresh();
  setInterval(refresh, 3000);
}

document.addEventListener("click", event => {
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
  const tabButton = closest(".admin-tab");
  if (tabButton) {
    setAdminTab(tabButton.dataset.adminTab);
    return;
  }
  const scheduleToggle = closest("button[data-schedule-toggle]");
  if (scheduleToggle) {
    toggleSchedule(
      Number(scheduleToggle.dataset.scheduleToggle),
      scheduleToggle.dataset.scheduleEnabled === "true",
    ).catch(error => showRuntimeStatus(error.message, "error"));
    return;
  }
  const scheduleDelete = closest("button[data-schedule-delete]");
  if (scheduleDelete) {
    deleteSchedule(Number(scheduleDelete.dataset.scheduleDelete))
      .catch(error => showRuntimeStatus(error.message, "error"));
    return;
  }
  const memoryToggle = closest("button[data-memory-toggle]");
  if (memoryToggle) {
    void toggleMemory(memoryToggle.dataset.memoryToggle);
    return;
  }
  const memoryEdit = closest("button[data-memory-edit]");
  if (memoryEdit) {
    if (expandedMemory && expandedMemory.name === memoryEdit.dataset.memoryEdit) {
      expandedMemory.editing = true;
      renderMemories(memoriesIndex);
    }
    return;
  }
  const memoryEditCancel = closest("button[data-memory-edit-cancel]");
  if (memoryEditCancel) {
    if (expandedMemory) {
      expandedMemory.editing = false;
      renderMemories(memoriesIndex);
    }
    return;
  }
  const memoryEditSave = closest("button[data-memory-edit-save]");
  if (memoryEditSave) {
    void saveMemoryWith(
      memoryEditSave.dataset.memoryEditSave,
      $("memory-edit-description").value.trim(),
      $("memory-edit-body").value,
      false,
    );
    return;
  }
  const memoryDelete = closest("button[data-memory-delete-name]");
  if (memoryDelete) {
    void deleteMemoryNamed(memoryDelete.dataset.memoryDeleteName);
    return;
  }
  const revertButton = closest("button[data-revert-id]");
  if (revertButton) {
    void revertCheckpoint(Number(revertButton.dataset.revertId));
    return;
  }
  const workspaceButton = closest("button[data-workspace-id]");
  if (workspaceButton) {
    const app = apps.find(candidate => candidate.thread_id === workspaceButton.dataset.workspaceId);
    if (app) void showApp(app).catch(error => showRuntimeStatus(error.message, "error"));
    return;
  }
  const card = closest(".app-card");
  if (!card) return;
  const app = apps.find(candidate => candidate.thread_id === card.dataset.appId);
  if (app) void showApp(app).catch(error => showRuntimeStatus(error.message, "error"));
});
$("new-app").addEventListener("click", () => createApp());
$("home-empty-primary").addEventListener("click", () => createApp());
$("workspace-new-app").addEventListener("click", () => createApp());
$("workspace-panel-close").addEventListener("click", () => setWorkspacePanelOpen(false, true));
$("workspace-panel-open").addEventListener("click", () => setWorkspacePanelOpen(true, true));
$("workspace-panel-backdrop").addEventListener("click", () => setWorkspacePanelOpen(false, true));
$("rename-app").addEventListener("click", () => renameSelectedApp().catch(error => showRuntimeStatus(error.message, "error")));
$("admin-open").addEventListener("click", () => openAdmin());
$("admin-close").addEventListener("click", () => closeAdmin());
$("activity-toggle").addEventListener("click", toggleActivity);
$("empty-primary").addEventListener("click", () => {
  if (!selectedAppId) return;
  openAdmin("chat");
});
$("schedule-form").addEventListener("submit", event => {
  submitSchedule(event).catch(error => showRuntimeStatus(error.message, "error"));
});
$("schedule-cadence").addEventListener("change", () => {
  const daily = $("schedule-cadence").value === "daily";
  $("schedule-interval-field").hidden = daily;
  $("schedule-time-field").hidden = !daily;
});
$("instructions-save").addEventListener("click", () => saveInstructions());
$("instructions-discard").addEventListener("click", () => discardInstructions());
$("instructions-editor").addEventListener("input", () => {
  $("instructions-status").textContent = "";
  syncInstructionsDirtyState();
});
$("memory-new").addEventListener("click", () => openMemoryEditor());
$("memory-cancel").addEventListener("click", () => {
  $("memory-editor").hidden = true;
});
$("memory-save").addEventListener("click", () => {
  void saveMemoryWith(
    $("memory-name").value.trim(),
    $("memory-description").value.trim(),
    $("memory-body").value,
    true,
  );
});
$("checkpoint-save").addEventListener("click", () => saveCheckpoint());
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
$("admin-overlay").addEventListener("keydown", event => {
  if (event.key !== "Tab") return;
  const focusable = Array.from($("admin-overlay").querySelectorAll(
    'button:not(:disabled), select:not(:disabled), textarea:not(:disabled), input:not(:disabled), [tabindex="0"]',
  )).filter(element => !element.hidden && element.offsetParent !== null);
  if (!focusable.length) return;
  const first = focusable[0];
  const last = focusable[focusable.length - 1];
  if (event.shiftKey && document.activeElement === first) {
    event.preventDefault();
    last.focus();
  } else if (!event.shiftKey && document.activeElement === last) {
    event.preventDefault();
    first.focus();
  }
});
$("admin-overlay").querySelector(".admin-nav").addEventListener("keydown", event => {
  if (!["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End"].includes(event.key)) return;
  const tabs = Array.from(document.querySelectorAll(".admin-tab"));
  const current = tabs.indexOf(document.activeElement);
  if (current < 0) return;
  event.preventDefault();
  let next = current;
  if (event.key === "Home") next = 0;
  else if (event.key === "End") next = tabs.length - 1;
  else if (event.key === "ArrowLeft" || event.key === "ArrowUp") {
    next = (current - 1 + tabs.length) % tabs.length;
  } else {
    next = (current + 1) % tabs.length;
  }
  setAdminTab(tabs[next].dataset.adminTab);
  tabs[next].focus();
});
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (workspacePanelOpen && matchMedia("(max-width: 720px)").matches) {
    event.preventDefault();
    setWorkspacePanelOpen(false, true);
    return;
  }
  if (!adminOpen) return;
  event.preventDefault();
  if (!$("memory-editor").hidden) {
    $("memory-editor").hidden = true;
    $("memory-new").focus();
  } else if (expandedMemory?.editing) {
    const name = expandedMemory.name;
    expandedMemory.editing = false;
    renderMemories(memoriesIndex);
    document.querySelector(`[data-memory-edit="${CSS.escape(name)}"]`)?.focus();
  } else {
    closeAdmin();
  }
});
window.addEventListener("resize", syncWorkspacePanel);
initialize();
