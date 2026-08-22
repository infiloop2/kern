// Entry module: session lifecycle (login, logout), tab switching, the
// 5-second refresh tick, and the one delegated click dispatcher that maps
// data-action buttons to feature handlers. Feature code lives in the sibling
// modules; this file is the only place that wires them together.

import {
  api, apiUpload, login as apiLogin, logout as apiLogout, setUnauthorizedHandler,
} from "./api.js";
import { $, notice, runtimeLabel } from "./helpers.js";
import {
  collapseRuntimeOverview, completeClaudeLogin, refreshHealth, refreshProviderAccounts,
  refreshProviderUsage, rebootHost, runtimeRecords, startLogin, toggleRuntimeOverview,
} from "./health.js";
import {
  dismissGettingStarted, refreshGettingStarted, STARTER_PROMPTS,
} from "./getting_started.js";
import {
  downloadViewedFile, ensureFilesLoaded, goToFilePath, loadParentDirectory,
  openAgentPath, openLinkedAgentFile,
  refreshFiles,
} from "./files.js";
import { refreshAgentProcesses } from "./processes.js";
import { agentLog, hostDiagnosticLog, netLog, toolLog, toggleHostDiagnosticFilter, toggleNetDeniedFilter } from "./logs.js";
import {
  addDomainRule, addGithubRepo, approveGithubPush, deleteGithubCredential,
  loadPolicy, recheckGithubAudit, rejectGithubPush, removeDomainRule,
  removeGithubRepo, resetLinkedAccount, connectBedrockCredentials, setProviderWebSearch, setGithubCredential, setGithubRequireApproval,
  setIntegrationEnabled, refreshPendingGithubPushes, toggleGithubCredentialMode,
  selectIntegrationDetail, toggleGithubRepoAudit,
} from "./network.js";
import {
  completeToolConnect, connectTool, decideToolApproval, disconnectTool,
  refreshExpandedToolApprovals, refreshTools, saveToolConfig, setToolEnabled,
  selectToolDetail,
} from "./tools.js";
import {
  copyCallbackUri, dismissCallbackCopyFeedback, openConnectionGuide, refreshConnectionGuide,
} from "./connection_guide.js";
import {
  finishPasskeyLogin, refreshLoginPasskeyStatus, refreshPasskeySetup,
  setupPasskey, showPasskeyGuidance,
} from "./passkeys.js";

// Panel navigation is pushState within one document, and every panel decides
// its own scroll position (see resetPageScroll). Leaving restoration on "auto"
// lets the browser also restore a remembered offset for the entry it is
// creating, asynchronously and after the panel has already reset — which opens
// a freshly navigated panel part-way down. Own the decision instead.
if ("scrollRestoration" in history) history.scrollRestoration = "manual";

if ("serviceWorker" in navigator && window.isSecureContext) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(() => {
      // Installation is progressive enhancement; the live admin UI remains
      // fully usable if a browser or private session declines registration.
    });
  }, { once: true });
}

const IOS_INSTALL_DISMISSED_AT = "kern.ios-install-dismissed-at.v1";
const IOS_INSTALL_REMIND_AFTER_MS = 30 * 24 * 60 * 60 * 1000;
let iosInstallTimer = null;
let iosInstallReturnFocus = null;

function isIPhoneStandalone() {
  return /iPhone/i.test(navigator.userAgent) && (
    window.matchMedia("(display-mode: standalone)").matches
    || window.navigator.standalone === true
  );
}

function shouldOfferIPhoneInstall() {
  if (!/iPhone/i.test(navigator.userAgent) || isIPhoneStandalone()) return false;
  try {
    const dismissedAt = Number(localStorage.getItem(IOS_INSTALL_DISMISSED_AT) || 0);
    return !dismissedAt || Date.now() - dismissedAt >= IOS_INSTALL_REMIND_AFTER_MS;
  } catch (_error) {
    return true;
  }
}

function hideIPhoneInstallUi() {
  $("ios-install-coach").hidden = true;
  $("ios-install-overlay").hidden = true;
  document.body.classList.remove("install-guide-open");
}

function scheduleIPhoneInstallCoach() {
  hideIPhoneInstallUi();
  if (iosInstallTimer) clearTimeout(iosInstallTimer);
  if (!shouldOfferIPhoneInstall()) return;
  iosInstallTimer = setTimeout(() => {
    iosInstallTimer = null;
    if (!$("app").hidden && shouldOfferIPhoneInstall()) $("ios-install-coach").hidden = false;
  }, 3500);
}

function showIPhoneInstallGuide(trigger) {
  iosInstallReturnFocus = trigger || null;
  $("ios-install-coach").hidden = true;
  $("ios-install-overlay").hidden = false;
  document.body.classList.add("install-guide-open");
  $("ios-install-done").focus();
}

function closeIPhoneInstallGuide() {
  $("ios-install-overlay").hidden = true;
  document.body.classList.remove("install-guide-open");
  if (iosInstallReturnFocus?.isConnected) iosInstallReturnFocus.focus();
  iosInstallReturnFocus = null;
  if (shouldOfferIPhoneInstall()) $("ios-install-coach").hidden = false;
}

function dismissIPhoneInstall() {
  try { localStorage.setItem(IOS_INSTALL_DISMISSED_AT, String(Date.now())); }
  catch (_error) { /* Dismissal remains valid for this page load. */ }
  hideIPhoneInstallUi();
  iosInstallReturnFocus = null;
}

let activeTab = "home";
let activeTabRefresh = Promise.resolve();
// Each panel opening gets a number, so a later one within the same tab -- a
// Home detail replacing another -- is distinguishable from the one whose
// refresh is resolving. Comparing tab names alone cannot see that.
let panelOpenSequence = 0;
// Whether the operator has asked to scroll since the current panel opened.
// Tracked from input intent rather than from scroll position, because the
// position moving is exactly what the reset below exists to correct: a layout
// shift produces a scroll event but none of these.
let operatorScrolledSincePanelOpen = false;
for (const event of ["wheel", "touchmove", "keydown"]) {
  window.addEventListener(event, () => { operatorScrolledSincePanelOpen = true; }, { passive: true });
}
const staticTabs = ["home", "processes", "agent-log", "files", "network", "net-log", "tool-log", "host-diagnostics"];
const homeDetailTabs = new Set(staticTabs.filter(name => name !== "home"));
const MOBILE_NAV_QUERY = "(max-width: 860px)";
let mobileNavOpen = false;
let uploadPickerOpen = false;
let nextUploadSelectionId = 1;
const APP_UPLOAD_SELECTION_LIMIT = 10;
const WORKSPACE_LAST_SEEN_KEY = "kern.workspace-last-seen.v2";
const pendingAppUploads = new Map();
let chatNavArchived = false;
let webAppsNavArchived = false;
let chatNavItems = [];
let webAppNavItems = [];
let workspaceNavigationRefreshSequence = 0;
let workspaceNavigationActionSequence = 0;
const workspacePendingMutations = new Set();
// Login preload and an immediate navigation click share the same mount.
const workspaceMounts = new Map();
let workspaceInitialization = null;
window.KernWorkspaceRoots = {};

function emptyWorkspaceLastSeen() {
  return {
    active: { chat: false, apps: false },
    archived: { chat: false, apps: false },
    chat: {},
    apps: {},
  };
}

function parseWorkspaceLastSeen(value) {
  if (!value) return emptyWorkspaceLastSeen();
  try {
    const parsed = JSON.parse(value);
    if (!parsed || typeof parsed !== "object") return emptyWorkspaceLastSeen();
    return {
      active: {
        chat: parsed.active?.chat === true,
        apps: parsed.active?.apps === true,
      },
      archived: {
        chat: parsed.archived?.chat === true,
        apps: parsed.archived?.apps === true,
      },
      chat: parsed.chat && typeof parsed.chat === "object" ? parsed.chat : {},
      apps: parsed.apps && typeof parsed.apps === "object" ? parsed.apps : {},
    };
  } catch (_error) {
    return emptyWorkspaceLastSeen();
  }
}

function loadWorkspaceLastSeen() {
  try { return parseWorkspaceLastSeen(localStorage.getItem(WORKSPACE_LAST_SEEN_KEY)); }
  catch (_error) { return emptyWorkspaceLastSeen(); }
}

let workspaceLastSeen = loadWorkspaceLastSeen();

function workspaceItemMarker(kind, item) {
  // Keep the persisted `activity` field for compatibility with existing
  // last-seen data, but advance it only for conversation messages. Agent
  // reasoning, tool calls, and other activity must not create an unread dot.
  const marker = { activity: Math.max(0, Number(item.latest_message_seq) || 0) };
  if (kind === "apps") marker.revision = Math.max(0, Number(item.revision) || 0);
  return marker;
}

function workspaceItemId(kind, item) {
  return kind === "chat" ? item.thread_id : item.app_id;
}

function mergeWorkspaceMarkers(kind, left = {}, right = {}) {
  if (!left || typeof left !== "object") left = {};
  if (!right || typeof right !== "object") right = {};
  const merged = {
    activity: Math.max(Number(left.activity) || 0, Number(right.activity) || 0),
  };
  if (kind === "apps") {
    merged.revision = Math.max(Number(left.revision) || 0, Number(right.revision) || 0);
  }
  return merged;
}

function mergeWorkspaceLastSeen(left, right) {
  const merged = emptyWorkspaceLastSeen();
  for (const kind of ["chat", "apps"]) {
    merged.active[kind] = left.active[kind] || right.active[kind];
    merged.archived[kind] = left.archived[kind] || right.archived[kind];
    for (const itemId of new Set([...Object.keys(left[kind]), ...Object.keys(right[kind])])) {
      merged[kind][itemId] = mergeWorkspaceMarkers(kind, left[kind][itemId], right[kind][itemId]);
    }
  }
  return merged;
}

function saveWorkspaceLastSeen() {
  try {
    // A storage event can arrive after another tab has already advanced a
    // different item. Re-read and merge immediately before the whole-map write
    // so this tab never knowingly replaces that newer marker with its snapshot.
    workspaceLastSeen = mergeWorkspaceLastSeen(loadWorkspaceLastSeen(), workspaceLastSeen);
    localStorage.setItem(WORKSPACE_LAST_SEEN_KEY, JSON.stringify(workspaceLastSeen));
  }
  catch (_error) { /* The markers remain valid for this page load. */ }
}

function initializeWorkspaceLastSeen(kind, items, archived) {
  if (archived || workspaceLastSeen.active[kind]) return;
  for (const item of items) {
    const itemId = workspaceItemId(kind, item);
    workspaceLastSeen[kind][itemId] = mergeWorkspaceMarkers(
      kind,
      workspaceLastSeen[kind][itemId],
      workspaceItemMarker(kind, item),
    );
  }
  workspaceLastSeen.active[kind] = true;
  saveWorkspaceLastSeen();
}

function initializeArchivedWorkspaceLastSeen(kind, items, archived) {
  if (!archived || workspaceLastSeen.archived[kind]) return;
  for (const item of items) {
    const itemId = workspaceItemId(kind, item);
    workspaceLastSeen[kind][itemId] = mergeWorkspaceMarkers(
      kind,
      workspaceLastSeen[kind][itemId],
      workspaceItemMarker(kind, item),
    );
  }
  workspaceLastSeen.archived[kind] = true;
  saveWorkspaceLastSeen();
}

function workspaceItemHasChanges(kind, item, archived) {
  if (!(archived ? workspaceLastSeen.archived[kind] : workspaceLastSeen.active[kind])) {
    return false;
  }
  const seen = workspaceLastSeen[kind][workspaceItemId(kind, item)];
  if (!seen || typeof seen !== "object") return true;
  const current = workspaceItemMarker(kind, item);
  return current.activity > (Number(seen.activity) || 0)
    || (kind === "apps" && current.revision > (Number(seen.revision) || 0));
}

function markWorkspaceSeen(kind, item) {
  if (!item || !["chat", "apps"].includes(kind) || document.visibilityState !== "visible") return;
  const itemId = workspaceItemId(kind, item);
  const route = workspaceRouteFromLocation();
  const expectedTab = kind === "chat" ? "workspace-chat" : "workspace-web-apps";
  if (!itemId || activeTab !== expectedTab || route?.itemId !== itemId) return;
  const current = workspaceItemMarker(kind, item);
  const seen = workspaceLastSeen[kind][itemId] || {};
  const next = {
    activity: Math.max(current.activity, Number(seen.activity) || 0),
  };
  if (kind === "apps") {
    next.revision = Math.max(current.revision, Number(seen.revision) || 0);
  }
  if (JSON.stringify(next) === JSON.stringify(seen)) return;
  workspaceLastSeen[kind][itemId] = next;
  saveWorkspaceLastSeen();
  renderWorkspaceNavigation();
}

window.addEventListener("storage", event => {
  if (event.key !== WORKSPACE_LAST_SEEN_KEY) return;
  if (event.newValue === null) workspaceLastSeen = emptyWorkspaceLastSeen();
  else {
    const incoming = parseWorkspaceLastSeen(event.newValue);
    workspaceLastSeen = mergeWorkspaceLastSeen(workspaceLastSeen, incoming);
    if (JSON.stringify(workspaceLastSeen) !== JSON.stringify(incoming)) saveWorkspaceLastSeen();
  }
  renderWorkspaceNavigation();
});

function showLoginError(message) {
  const element = $("login-error");
  element.textContent = message;
  element.hidden = false;
}

function clearLegacyPasswordCookie() {
  // Pre-0.44 UIs stored the cleartext admin password in this readable cookie.
  document.cookie = "kern_admin=; path=/; max-age=0; samesite=strict";
}

async function login() {
  const value = $("password").value.trim();
  if (!value) return;
  $("login-error").hidden = true;
  let response;
  try {
    response = await apiLogin(value);
  } catch (_) {
    showLoginError("Could not reach the host. Try again.");
    return;
  }
  if (response.ok) {
    const result = await response.json();
    if (result.passkey_required) {
      try {
        response = await finishPasskeyLogin(result.publicKey);
      } catch (error) {
        showLoginError(
          error && error.name === "NotAllowedError"
            ? "Passkey verification was cancelled."
            : error.message || "Passkey verification failed.",
        );
        return;
      }
      if (!response.ok) {
        let message = "Passkey verification failed.";
        try {
          const failure = await response.json();
          message = failure.error?.message || message;
        } catch (_) {}
        showLoginError(message);
        return;
      }
    }
    $("password").value = "";
    $("login-error").hidden = true;
    showApp();
    return;
  }
  if (response.status === 429) {
    showLoginError("Too many attempts. Wait a few minutes and try again.");
  } else if (response.status === 401) {
    showLoginError("Incorrect password.");
  } else {
    let message = "Login failed. Try again.";
    try {
      const failure = await response.json();
      message = failure.error?.message || message;
    } catch (_) {}
    showLoginError(message);
  }
}

async function logout() {
  try {
    await apiLogout();
  } catch (_) {
    // The session cookie is HttpOnly, so a reload cannot clear it on its own;
    // still reload to return to the login screen even if the call failed.
  }
  location.reload();
}

function setMobileNavOpen(open, restoreFocus = false) {
  const mobile = window.matchMedia(MOBILE_NAV_QUERY).matches;
  mobileNavOpen = mobile && open;
  const sidebar = $("sidebar");
  const toggle = $("mobile-nav-toggle");
  sidebar.classList.toggle("mobile-open", mobileNavOpen);
  sidebar.inert = mobile && !mobileNavOpen;
  document.querySelector(".topbar").inert = mobileNavOpen;
  document.querySelector("main").inert = mobileNavOpen;
  $("nav-backdrop").hidden = !mobileNavOpen;
  toggle.setAttribute("aria-expanded", String(mobileNavOpen));
  toggle.setAttribute("aria-label", mobileNavOpen ? "Close navigation" : "Open navigation");
  document.body.classList.toggle("nav-open", mobileNavOpen);
  if (mobileNavOpen) {
    $("mobile-nav-close").focus();
  } else if (restoreFocus && mobile) {
    toggle.focus();
  }
}

function toggleMobileNav() {
  setMobileNavOpen(!mobileNavOpen, mobileNavOpen);
}

function bindIPhoneStandaloneSidebarSwipe() {
  if (!isIPhoneStandalone()) return;
  document.documentElement.classList.add("iphone-standalone");
  syncIPhoneStandaloneViewport();

  const edgeWidth = 32;
  const openDistance = 64;
  let swipe = null;

  document.addEventListener("touchstart", event => {
    swipe = null;
    if (
      mobileNavOpen
      || $("app").hidden
      || document.body.classList.contains("install-guide-open")
      || !window.matchMedia(MOBILE_NAV_QUERY).matches
      || event.touches.length !== 1
    ) return;
    const touch = event.touches[0];
    const edge = touch.clientX <= edgeWidth
      ? "left"
      : touch.clientX >= window.innerWidth - edgeWidth ? "right" : null;
    if (!edge) return;
    // Do not cancel touchstart: edge controls still receive ordinary taps.
    // Claim only a later, clearly horizontal move so vertical scrolling and
    // control activation remain native while iOS history navigation does not.
    swipe = { edge, identifier: touch.identifier, x: touch.clientX, y: touch.clientY };
  }, { capture: true, passive: false });

  document.addEventListener("touchmove", event => {
    if (!swipe) return;
    const touch = [...event.touches].find(item => item.identifier === swipe.identifier);
    if (!touch) {
      swipe = null;
      return;
    }
    const direction = swipe.edge === "left" ? 1 : -1;
    const horizontal = (touch.clientX - swipe.x) * direction;
    const vertical = Math.abs(touch.clientY - swipe.y);
    if (horizontal < 0 || vertical > Math.max(24, horizontal)) {
      swipe = null;
      return;
    }
    // Claim both iOS history edges. The left edge doubles as Kern's drawer
    // gesture; the right edge is swallowed without changing application state.
    if (horizontal < 10 || horizontal <= vertical) return;
    if (event.cancelable) event.preventDefault();
    if (swipe.edge !== "left" || horizontal < openDistance) return;
    swipe = null;
    setMobileNavOpen(true);
  }, { capture: true, passive: false });

  for (const eventName of ["touchend", "touchcancel"]) {
    document.addEventListener(eventName, () => { swipe = null; }, { capture: true });
  }
}

let iPhoneStandaloneViewportBaseline = null;
let workspaceKeyboardViewportBaselineHeight = 0;
function layoutViewportSize() {
  return {
    height: document.documentElement.clientHeight || window.innerHeight,
    width: document.documentElement.clientWidth || window.innerWidth,
  };
}

function syncIPhoneStandaloneViewport() {
  if (!isIPhoneStandalone()) return;
  const layout = layoutViewportSize();
  const orientationChanged = Boolean(
    iPhoneStandaloneViewportBaseline
    && Math.abs(layout.width - iPhoneStandaloneViewportBaseline.width) > 80
  );
  const keyboardOwnsViewport = workspaceKeyboardOwnsViewport();
  if (keyboardOwnsViewport && !orientationChanged) return;
  // iOS can leave dynamic viewport units at the keyboard-reduced height after
  // a standalone app transition. Use its height only while no software keyboard
  // owns it: resizing the layout to the keyboard and letting iOS pan the focused
  // input at the same time moves the composer twice and leaves it at the top.
  // Rotation changes the layout width, so use its new full height even if the
  // keyboard remains open instead of retaining the other orientation's pixels.
  const height = keyboardOwnsViewport
    ? layout.height
    : window.visualViewport?.height || layout.height;
  if (orientationChanged && keyboardOwnsViewport) {
    workspaceKeyboardViewportBaselineHeight = layout.height;
  }
  iPhoneStandaloneViewportBaseline = { height, width: layout.width };
  document.documentElement.style.setProperty("--kern-viewport-height", `${height}px`);
}

function resetPageScroll() {
  // `html` sets scroll-behavior: smooth, and an animation already in flight
  // keeps running across a later "instant" scroll — so a reset issued while
  // the page is still gliding gets overwritten a frame or two afterwards and
  // the panel opens part-way down. Dropping the property for the duration of
  // the jump cancels the running animation instead of racing it.
  const root = document.documentElement;
  const previous = root.style.scrollBehavior;
  root.style.scrollBehavior = "auto";
  window.scrollTo({ top: 0, left: 0, behavior: "instant" });
  root.style.scrollBehavior = previous;
}

let workspaceViewportRecovery = 0;
function visualViewportIsContracted() {
  const viewport = window.visualViewport;
  return Boolean(
    viewport
    && workspaceKeyboardViewportBaselineHeight
    && viewport.height < workspaceKeyboardViewportBaselineHeight - 80
  );
}

function workspaceKeyboardOwnsViewport() {
  if (document.body.classList.contains("workspace-input-focused")
      || isWorkspaceKeyboardInput(deepActiveElement())) return true;
  if (visualViewportIsContracted()) return true;
  // The viewport has expanded after focus ended, so later same-width changes
  // are real layout changes rather than the tail of this keyboard session.
  workspaceKeyboardViewportBaselineHeight = 0;
  return false;
}

function workspaceViewportCanRecover() {
  return document.body.classList.contains("viewport-panel-open")
    && !workspaceKeyboardOwnsViewport()
    && window.matchMedia(MOBILE_NAV_QUERY).matches;
}

function recoverWorkspaceViewport() {
  if (!workspaceViewportCanRecover()) return;
  cancelAnimationFrame(workspaceViewportRecovery);
  workspaceViewportRecovery = requestAnimationFrame(() => {
    workspaceViewportRecovery = requestAnimationFrame(() => {
      // Focus can return while these frames are queued. Re-check immediately
      // before scrolling so a stale keyboard-close recovery cannot move the
      // page out from under the field the operator is typing in.
      if (!workspaceViewportCanRecover()) return;
      /* Mobile Safari may pan the layout viewport to expose a focused field.
         It does not always restore that pan when Send clears the field or the
         keyboard closes. The workspaces own the visual viewport, so the page
         itself must remain anchored while their internal scrollers move. */
      syncIPhoneStandaloneViewport();
      resetPageScroll();
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
    });
  });
}

function deepActiveElement() {
  // Apps render inside the Workspace shadow root and then a generated-App
  // shadow root. Safari can retarget focus events at either host, so consult
  // the live focus chain before forcing the outer page back to the top.
  let active = document.activeElement;
  while (active?.shadowRoot?.activeElement) active = active.shadowRoot.activeElement;
  return active;
}

function isWorkspaceKeyboardInput(target) {
  return target instanceof HTMLTextAreaElement || (
    target instanceof HTMLInputElement
    && ["text", "search", "email", "tel", "url", "password", "number"].includes(target.type)
  );
}

let focusedWorkspaceInput = null;
const focusedWorkspaceInputObserver = new MutationObserver(() => {
  if (!focusedWorkspaceInput || focusedWorkspaceInput.isConnected) return;
  releaseWorkspaceInputFocus();
  recoverWorkspaceViewport();
  setTimeout(recoverWorkspaceViewport, 180);
});

function releaseWorkspaceInputFocus() {
  focusedWorkspaceInput = null;
  focusedWorkspaceInputObserver.disconnect();
  document.body.classList.remove("workspace-input-focused");
}

document.addEventListener("focusin", event => {
  const target = event.composedPath()[0];
  if (!isWorkspaceKeyboardInput(target)) return;
  if (!document.body.classList.contains("viewport-panel-open")) return;
  const currentHeight = window.visualViewport?.height || layoutViewportSize().height;
  workspaceKeyboardViewportBaselineHeight = iPhoneStandaloneViewportBaseline?.height
    || Math.max(workspaceKeyboardViewportBaselineHeight, currentHeight);
  focusedWorkspaceInput = target;
  focusedWorkspaceInputObserver.disconnect();
  focusedWorkspaceInputObserver.observe(target.getRootNode(), { childList: true, subtree: true });
  document.body.classList.add("workspace-input-focused");
}, true);

document.addEventListener("focusout", event => {
  const target = event.composedPath()[0];
  if (!isWorkspaceKeyboardInput(target)) return;
  setTimeout(() => {
    if (target !== focusedWorkspaceInput) return;
    releaseWorkspaceInputFocus();
    recoverWorkspaceViewport();
    setTimeout(recoverWorkspaceViewport, 180);
  }, 0);
}, true);
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", syncIPhoneStandaloneViewport, { passive: true });
  window.visualViewport.addEventListener("resize", recoverWorkspaceViewport, { passive: true });
}

function showLogin() {
  setMobileNavOpen(false);
  hideIPhoneInstallUi();
  releaseWorkspaceInputFocus();
  document.body.classList.remove("viewport-panel-open", "workspace-input-focused");
  $("login").hidden = false;
  $("app").hidden = true;
  $("logout-button").hidden = true;
  $("agent-name").hidden = true;
  $("runtime-overview").hidden = true;
  $("passkey-status-control").hidden = true;
  $("mobile-nav-toggle").hidden = true;
  refreshLoginPasskeyStatus();
}

function showTab(name, workspaceActionSequence = null) {
  if (workspaceActionSequence === null) {
    workspaceNavigationActionSequence += 1;
  } else if (workspaceActionSequence !== workspaceNavigationActionSequence) {
    return false;
  }
  const closeDrawer = mobileNavOpen;
  activeTab = name;
  const workspaceOpen = name === "workspace-chat" || name === "workspace-web-apps"
    || name === "workspace-global";
  const viewportPanelOpen = workspaceOpen;
  document.body.classList.toggle("viewport-panel-open", viewportPanelOpen);
  syncIPhoneStandaloneViewport();
  if (!viewportPanelOpen) {
    releaseWorkspaceInputFocus();
  }
  for (const tabName of staticTabs) {
    const tab = document.getElementById(`tab-${tabName}`);
    if (tab) tab.classList.toggle("active-tab", tabName === name || (tabName === "home" && homeDetailTabs.has(name)));
    $(`panel-${tabName}`).hidden = tabName !== name;
  }
  $("panel-workspace-chat").hidden = name !== "workspace-chat";
  $("panel-workspace-web-apps").hidden = name !== "workspace-web-apps";
  $("panel-workspace-global").hidden = name !== "workspace-global";
  renderWorkspaceNavigation();
  setMobileNavOpen(false, closeDrawer);
  const opensAtTop = viewportPanelOpen || name === "home" || homeDetailTabs.has(name);
  const openSequence = ++panelOpenSequence;
  if (opensAtTop) {
    operatorScrolledSincePanelOpen = false;
    resetPageScroll();
  }
  // A panel has not finished opening when showTab returns -- its refresh is
  // still in flight, and the content that lands can move the page out from
  // under the reset above. Which mechanism moves it varies by engine, so
  // rather than chase each one, re-assert the position at the point the panel
  // is actually open.
  //
  // Only for the panel that is still open and untouched, though. A slow
  // refresh leaves the panel visible and interactive -- the network tab runs
  // four enter refreshers in sequence -- so an operator can be reading part
  // way down by the time this resolves, and snapping them back would be worse
  // than the misplaced scroll it fixes.
  activeTabRefresh = refreshVisibleTab(name).then(() => {
    if (!opensAtTop) return;
    if (openSequence !== panelOpenSequence) return;
    if (operatorScrolledSincePanelOpen) return;
    resetPageScroll();
  }).catch(() => {});
  return true;
}

function homeRouteUrl(view = "home", guideId = "") {
  if (view === "network" && guideId) return `#home/integrations/${encodeURIComponent(guideId)}`;
  return view === "home" ? "#home" : `#home/${encodeURIComponent(view)}`;
}

function homeRouteFromLocation() {
  const integrationMatch = location.hash.match(/^#home\/integrations\/(.+)$/);
  if (integrationMatch) {
    try {
      return { view: "network", guideId: decodeURIComponent(integrationMatch[1]) };
    } catch (_) {
      return { view: "home", guideId: "" };
    }
  }
  const viewMatch = location.hash.match(/^#home\/([^/]+)$/);
  if (viewMatch) {
    try {
      const view = decodeURIComponent(viewMatch[1]);
      if (homeDetailTabs.has(view)) return { view, guideId: "" };
    } catch (_) {}
  }
  return { view: "home", guideId: "" };
}

function recordHomeRoute(view, guideId = "", replace = false) {
  const state = { kernHomeRoute: view, guideId };
  history[replace ? "replaceState" : "pushState"](state, "", homeRouteUrl(view, guideId));
}

function workspaceRouteUrl(resource, itemId = null) {
  if (resource === "chat") {
    return itemId ? `#chat/${encodeURIComponent(itemId)}` : "#chat/new";
  }
  if (resource === "apps") {
    return itemId ? `#apps/${encodeURIComponent(itemId)}` : "#apps";
  }
  if (resource === "memory" || resource === "schedules") {
    return itemId ? `#${resource}/${encodeURIComponent(itemId)}` : `#${resource}`;
  }
  return "#home";
}

function workspaceRouteFromLocation() {
  const match = location.hash.match(/^#(chat|apps|memory|schedules)(?:\/(.+))?$/);
  if (!match) return null;
  try {
    const resource = match[1];
    const encodedItemId = match[2] || "";
    if (resource === "chat" && encodedItemId === "new") {
      return { resource, itemId: null };
    }
    if (resource === "chat" && !encodedItemId) return null;
    return { resource, itemId: encodedItemId ? decodeURIComponent(encodedItemId) : null };
  } catch (_) {
    return null;
  }
}

function recordWorkspaceRoute(resource, itemId = null, replace = false) {
  const state = { kernWorkspaceRoute: resource, itemId };
  history[replace ? "replaceState" : "pushState"](
    state,
    "",
    workspaceRouteUrl(resource, itemId),
  );
}

function navigateWorkspaceRoute(resource, itemId = null, replace = false) {
  const url = workspaceRouteUrl(resource, itemId);
  const current = history.state;
  if (location.hash === url) {
    if (current?.kernWorkspaceRoute === resource && current.itemId === itemId) return false;
    recordWorkspaceRoute(resource, itemId, true);
    return true;
  }
  recordWorkspaceRoute(resource, itemId, replace);
  return true;
}

function openHomeView(view, guideId = "", updateHistory = true) {
  if (!homeDetailTabs.has(view)) return;
  if (view === "network") {
    selectToolDetail(guideId);
    selectIntegrationDetail(guideId);
    openConnectionGuide(guideId);
  }
  if (updateHistory) {
    if (!history.state?.kernHomeRoute && !history.state?.kernWorkspaceRoute) {
      recordHomeRoute("home", "", true);
    }
    recordHomeRoute(view, guideId);
  }
  showTab(view);
}

function openHomeIntegration(guideId, updateHistory = true) {
  if (!guideId) return;
  openHomeView("network", guideId, updateHistory);
}

function backToHome(workspaceActionSequence = null) {
  if (
    workspaceActionSequence !== null
    && workspaceActionSequence !== workspaceNavigationActionSequence
  ) return false;
  if (history.state?.kernHomeRoute !== "home" || location.hash !== "#home") {
    recordHomeRoute("home");
  }
  return showTab("home", workspaceActionSequence);
}

function openPasskeyGuidance() {
  const control = $("passkey-status-control");
  if (control.dataset.configured === "true") {
    showPasskeyGuidance();
    return;
  }
  backToHome();
  showPasskeyGuidance();
  setupPasskey();
}

// Each tab's refreshers: "enter" runs when the tab is shown, "tick" runs on
// the 5-second poll while the tab stays visible. Log tabs refresh on the tick
// only while their first page is showing, so paging back stays stable. Tool
// rows hold config inputs, so they refresh on tab entry and after actions
// only, never on the tick (that would wipe half-typed values); expanded
// approvals carry no inputs and also refresh on the tick.
const tabRefreshers = {
  "home": { enter: [refreshConnectionGuide], tick: [] },
  "agent-log": {
    enter: [() => agentLog.showFirstPage()],
    tick: [() => agentLog.page === 1 && agentLog.showFirstPage()],
  },
  "net-log": {
    enter: [() => netLog.showFirstPage()],
    tick: [() => netLog.page === 1 && netLog.showFirstPage()],
  },
  "tool-log": {
    enter: [() => toolLog.showFirstPage()],
    tick: [() => toolLog.page === 1 && toolLog.showFirstPage()],
  },
  "host-diagnostics": {
    enter: [() => hostDiagnosticLog.showFirstPage()],
    tick: [() => hostDiagnosticLog.page === 1 && hostDiagnosticLog.showFirstPage()],
  },
  "processes": { enter: [refreshAgentProcesses], tick: [refreshAgentProcesses] },
  "files": { enter: [ensureFilesLoaded], tick: [refreshFiles] },
  "network": {
    enter: [loadPolicy, refreshTools, refreshExpandedToolApprovals, refreshConnectionGuide],
    tick: [refreshPendingGithubPushes, refreshExpandedToolApprovals],
  },
};

async function refreshVisibleTab(name) {
  for (const refresh of tabRefreshers[name]?.enter || []) await refresh();
}

async function tick() {
  await refreshOrSkip(refreshHealth);
  await refreshOrSkip(refreshProviderAccounts);
  await refreshOrSkip(refreshWorkspaceNavigation);
  await refreshOrSkip(() => refreshGettingStarted());
  for (const refresh of tabRefreshers[activeTab]?.tick || []) await refreshOrSkip(refresh);
}

async function refreshOrSkip(work) {
  try {
    await work();
  } catch (_error) {
    // Keep one failed dashboard section from preventing later sections, such
    // as the audit logs, from fetching their own backend state.
  }
}

let appStarted = false;

// The session cookie is HttpOnly, so login state cannot be read in JS; probe an
// authenticated endpoint instead. A 401 lands on the login screen.
async function start() {
  clearLegacyPasswordCookie();
  try {
    await api("GET", "/v1/health");
  } catch (_) {
    showLogin();
    return;
  }
  showApp();
}

function showApp() {
  $("login").hidden = true;
  $("app").hidden = false;
  $("logout-button").hidden = false;
  $("mobile-nav-toggle").hidden = false;
  $("runtime-overview").hidden = false;
  setMobileNavOpen(false);
  const workspaceReady = initializeWorkspaces();
  workspaceReady.catch(error => notice(error.message, "error"));
  refreshPasskeySetup();
  scheduleIPhoneInstallCoach();
  loadPolicy().catch(() => {});
  // The provider redirects a tool OAuth connect back to /oauth/callback;
  // finish the exchange and return to that integration's Home detail page.
  if (location.pathname === "/oauth/callback") {
    const callbackSearch = location.search;
    const pendingTool = sessionStorage.getItem("kern_tool_connect");
    history.replaceState(null, "", "/");
    if (pendingTool) {
      const guideId = `tool:${pendingTool}`;
      recordHomeRoute("network", guideId, true);
      openHomeIntegration(guideId, false);
    } else {
      recordHomeRoute("home", "", true);
      showTab("home");
    }
    activeTabRefresh
      .then(() => completeToolConnect(callbackSearch))
      .catch(error => notice(error.message, "error"));
  } else {
    const workspaceRoute = workspaceRouteFromLocation();
    if (workspaceRoute) {
      // Preserve copied deep links even if the first Workspace mount/index
      // request fails before the asynchronous restore can begin.
      navigateWorkspaceRoute(workspaceRoute.resource, workspaceRoute.itemId, true);
      showTab("home");
      void workspaceReady
        .then(() => {
          const currentRoute = workspaceRouteFromLocation();
          if (
            currentRoute?.resource !== workspaceRoute.resource
            || currentRoute.itemId !== workspaceRoute.itemId
          ) return;
          return restoreWorkspaceRoute(workspaceRoute);
        })
        .catch(error => notice(error.message, "error"));
    } else {
      if (!history.state?.kernHomeRoute) {
        const locationRoute = homeRouteFromLocation();
        recordHomeRoute(locationRoute.view, locationRoute.guideId, true);
      }
      const route = history.state?.kernHomeRoute;
      if (route && route !== "home" && homeDetailTabs.has(route)) {
        openHomeView(route, history.state.guideId || "", false);
      } else {
        showTab("home");
      }
    }
  }
  // Guard the recurring tick so a re-login within the same page load (the login
  // screen never reloads on success) cannot stack a second interval.
  if (appStarted) return;
  appStarted = true;
  tick();
  setInterval(tick, 5000);
}

window.KernHost = {
  api,
  apiUpload,
  refreshNavigation() {
    return refreshWorkspaceNavigation();
  },
  markWorkspaceSeen,
  navigateWorkspace(resource, itemId = null, replace = false) {
    if (navigateWorkspaceRoute(resource, itemId, replace)) {
      workspaceNavigationActionSequence += 1;
    }
  },
  openAgentFile(path, fallbackPath = "") {
    openHomeView("files");
    return openLinkedAgentFile(path, fallbackPath);
  },
  chooseFiles(maximum = 10) {
    return new Promise(resolve => {
      const input = document.createElement("input");
      input.type = "file";
      input.multiple = maximum > 1;
      input.className = "host-file-picker";
      document.body.append(input);
      const finish = files => {
        input.remove();
        // Return the complete selection so the owning workspace can reject
        // an over-limit choice instead of silently accepting its first files.
        // `maximum` still controls whether the native picker permits multiple
        // selection when only one slot remains.
        resolve(files && files.length ? files : null);
      };
      input.addEventListener("change", () => finish(Array.from(input.files || [])), { once: true });
      input.addEventListener("cancel", () => finish(null), { once: true });
      input.click();
    });
  },
};

async function mountWorkspaces() {
  await mountWorkspace("chat", "panel-workspace-chat", "/workspace/chat.html");
  await mountWorkspace("web-apps", "panel-workspace-web-apps", "/workspace/web-apps.html");
  await mountWorkspace("global", "panel-workspace-global", "/workspace/global.html");
}

function initializeWorkspaces() {
  if (!workspaceInitialization) {
    workspaceInitialization = mountWorkspaces().catch(error => {
      workspaceInitialization = null;
      throw error;
    });
    // Sidebar indexes are helpful context, but they are not a prerequisite
    // for mounting any Workspace. In particular, Chat/App index failures must
    // not block independent Memory or Schedule routes.
    void workspaceInitialization.then(
      () => refreshWorkspaceNavigation().catch(error => notice(error.message, "error")),
      () => {},
    );
  }
  return workspaceInitialization;
}

async function mountWorkspace(name, panelId, htmlPath) {
  let mounting = workspaceMounts.get(name);
  if (!mounting) {
    mounting = performWorkspaceMount(name, panelId, htmlPath);
    workspaceMounts.set(name, mounting);
  }
  await mounting;
}

async function performWorkspaceMount(name, panelId, htmlPath) {
  const response = await fetch(htmlPath, { credentials: "same-origin" });
  if (!response.ok) throw new Error(`Could not load workspace ${name}`);
  const parsed = new DOMParser().parseFromString(await response.text(), "text/html");
  const root = parsed.body.firstElementChild;
  if (!root) throw new Error(`workspace ${name} UI is empty`);
  const shadow = $(panelId).shadowRoot || $(panelId).attachShadow({ mode: "open" });
  addWorkspaceStyle(shadow, "/admin_ui.css");
  addWorkspaceStyle(shadow, "/workspace/rich_text.css");
  const assetName = name === "chat" ? "chat" : name === "web-apps" ? "web-apps" : "global";
  addWorkspaceStyle(shadow, `/workspace/${assetName}.css`);
  shadow.append(root);
  window.KernWorkspaceRoots[name] = shadow;
  if (!window.KernRichText) {
    await loadWorkspaceScript("/workspace/rich_text.js");
  }
  await loadWorkspaceScript(`/workspace/${assetName}.js`);
}

function addWorkspaceStyle(root, href) {
  const link = document.createElement("link");
  link.rel = "stylesheet";
  link.href = href;
  root.append(link);
}

function loadWorkspaceScript(src) {
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = src;
    script.addEventListener("load", resolve, { once: true });
    script.addEventListener("error", () => reject(new Error(`Could not load ${src}`)), { once: true });
    document.body.append(script);
  });
}

async function refreshWorkspaceNavigation() {
  const sequence = ++workspaceNavigationRefreshSequence;
  const chatArchived = chatNavArchived;
  const webAppsArchived = webAppsNavArchived;
  let chat;
  let webApps;
  try {
    [chat, webApps] = await Promise.all([
      api("GET", chatArchived ? "/v1/workspace/chat/threads?archived=true" : "/v1/workspace/chat/threads"),
      api("GET", webAppsArchived ? "/v1/workspace/web-apps/apps?archived=true" : "/v1/workspace/web-apps/apps"),
    ]);
  } catch (error) {
    if (
      sequence !== workspaceNavigationRefreshSequence
      || chatArchived !== chatNavArchived
      || webAppsArchived !== webAppsNavArchived
    ) return;
    throw error;
  }
  if (
    sequence !== workspaceNavigationRefreshSequence
    || chatArchived !== chatNavArchived
    || webAppsArchived !== webAppsNavArchived
  ) return;
  chatNavItems = chat.threads || [];
  webAppNavItems = webApps.apps || [];
  initializeWorkspaceLastSeen("chat", chatNavItems, chatNavArchived);
  initializeWorkspaceLastSeen("apps", webAppNavItems, webAppsNavArchived);
  initializeArchivedWorkspaceLastSeen("chat", chatNavItems, chatNavArchived);
  initializeArchivedWorkspaceLastSeen("apps", webAppNavItems, webAppsNavArchived);
  renderWorkspaceNavigation();
}

function renderWorkspaceNavigation() {
  renderWorkspaceRows("chat-nav-items", chatNavItems, "open-chat", chatNavArchived);
  renderWorkspaceRows("web-apps-nav-items", webAppNavItems, "open-web-app", webAppsNavArchived);
  const chatArchive = document.querySelector('[data-action="show-chat-archive"]');
  const appArchive = document.querySelector('[data-action="show-web-app-archive"]');
  if (chatArchive) {
    chatArchive.textContent = chatNavArchived ? "Active" : "Archived";
    chatArchive.setAttribute("aria-pressed", String(chatNavArchived));
  }
  if (appArchive) {
    appArchive.textContent = webAppsNavArchived ? "Active" : "Archived";
    appArchive.setAttribute("aria-pressed", String(webAppsNavArchived));
  }
  for (const resource of ["memory", "schedules"]) {
    $(`tab-workspace-${resource}`).classList.toggle(
      "active-tab",
      activeTab === "workspace-global" && window.KernWorkspaceGlobal?.resource === resource,
    );
  }
}

async function openWorkspaceGlobal(resource, itemId = null, updateHistory = true) {
  const actionSequence = ++workspaceNavigationActionSequence;
  await initializeWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return;
  if (!showTab("workspace-global", actionSequence)) return;
  if (updateHistory) navigateWorkspaceRoute(resource, itemId);
  const opened = await window.KernWorkspaceGlobal.open(resource, itemId);
  renderWorkspaceNavigation();
  return opened;
}

function renderWorkspaceRows(containerId, items, action, archived) {
  const container = $(containerId);
  container.replaceChildren();
  for (const item of items) {
    const kind = action === "open-chat" ? "chat" : "web-apps";
    const itemId = kind === "chat" ? item.thread_id : item.app_id;
    const pending = workspacePendingMutations.has(`${kind}:${itemId}`);
    const row = document.createElement("div");
    row.className = "workspace-nav-row";
    const button = document.createElement("button");
    button.className = "workspace-nav-item";
    button.dataset.action = action;
    button.dataset.itemId = itemId;
    button.disabled = pending;
    const primary = document.createElement("span");
    primary.className = "workspace-nav-primary";
    if (item.status === "running") {
      const dot = document.createElement("span");
      dot.className = "workspace-nav-running";
      dot.setAttribute("aria-label", "Agent running");
      primary.append(dot);
    }
    const label = document.createElement("span");
    label.className = "workspace-nav-label";
    label.textContent = item.name || itemId;
    primary.append(label);
    const hasChanges = workspaceItemHasChanges(
      kind === "chat" ? "chat" : "apps",
      item,
      archived,
    );
    if (hasChanges) {
      const dot = document.createElement("span");
      dot.className = "workspace-nav-unseen";
      dot.setAttribute("role", "img");
      dot.setAttribute("aria-label", "New activity");
      primary.append(dot);
    }
    button.append(primary);
    if (kind === "chat") {
      const settings = [runtimeLabel(item.agent_runtime), item.model, item.effort]
        .filter(Boolean)
        .join(" · ");
      const meta = document.createElement("span");
      meta.className = "workspace-nav-meta";
      meta.textContent = settings;
      button.append(meta);
      const detail = settings ? `${item.name || itemId}\n${settings}` : item.name || itemId;
      button.title = hasChanges ? `${detail}\nNew activity` : detail;
    } else {
      button.title = hasChanges ? `${item.name || itemId}\nNew activity` : item.name || itemId;
    }
    row.append(button);
    if (archived) {
      const restore = document.createElement("button");
      restore.className = "workspace-nav-row-action";
      restore.dataset.action = action === "open-chat" ? "unarchive-chat" : "unarchive-web-app";
      restore.dataset.itemId = itemId;
      restore.disabled = pending;
      restore.title = "Restore";
      restore.setAttribute("aria-label", `Restore ${item.name || itemId}`);
      restore.textContent = "↩";
      row.append(restore);
    }
    container.append(row);
  }
}

async function findChatNavItem(threadId) {
  let thread = chatNavItems.find(item => item.thread_id === threadId);
  if (thread) return { item: thread, archived: chatNavArchived, items: chatNavItems };
  for (const archived of [false, true]) {
    const response = await api(
      "GET",
      archived ? "/v1/workspace/chat/threads?archived=true" : "/v1/workspace/chat/threads",
    );
    const items = response.threads || [];
    thread = items.find(item => item.thread_id === threadId);
    if (!thread) continue;
    return { item: thread, archived, items };
  }
  return null;
}

async function openWorkspaceNewChat(updateHistory = true, prompt = "") {
  const actionSequence = ++workspaceNavigationActionSequence;
  await initializeWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return false;
  chatNavArchived = false;
  if (!showTab("workspace-chat", actionSequence)) return false;
  if (updateHistory) navigateWorkspaceRoute("chat");
  window.KernChat.newThread(prompt);
  await refreshWorkspaceNavigation();
  return true;
}

async function openWorkspaceChat(threadId, updateHistory = true) {
  if (workspacePendingMutations.has(`chat:${threadId}`)) return;
  const actionSequence = ++workspaceNavigationActionSequence;
  await initializeWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return;
  const found = await findChatNavItem(threadId);
  if (actionSequence !== workspaceNavigationActionSequence) return;
  if (!found) return false;
  chatNavArchived = found.archived;
  chatNavItems = found.items;
  renderWorkspaceNavigation();
  if (!showTab("workspace-chat", actionSequence)) return;
  if (updateHistory) navigateWorkspaceRoute("chat", threadId);
  try {
    await window.KernChat.openThread(found.item);
  } catch (error) {
    if (actionSequence === workspaceNavigationActionSequence) throw error;
  }
  return true;
}

async function findWebAppNavItem(appId) {
  let selected = webAppNavItems.find(item => item.app_id === appId);
  if (selected) {
    return { item: selected, archived: webAppsNavArchived, items: webAppNavItems };
  }
  for (const archived of [false, true]) {
    const response = await api(
      "GET",
      archived ? "/v1/workspace/web-apps/apps?archived=true" : "/v1/workspace/web-apps/apps",
    );
    const items = response.apps || [];
    selected = items.find(item => item.app_id === appId);
    if (!selected) continue;
    return { item: selected, archived, items };
  }
  return null;
}

async function openWorkspaceWebApp(appId, updateHistory = true) {
  if (workspacePendingMutations.has(`web-apps:${appId}`)) return;
  const actionSequence = ++workspaceNavigationActionSequence;
  await initializeWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return;
  const found = await findWebAppNavItem(appId);
  if (actionSequence !== workspaceNavigationActionSequence) return;
  if (!found) return false;
  webAppsNavArchived = found.archived;
  webAppNavItems = found.items;
  renderWorkspaceNavigation();
  if (!showTab("workspace-web-apps", actionSequence)) return;
  if (updateHistory) navigateWorkspaceRoute("apps", appId);
  try {
    await window.KernWebApps.open(found.item, webAppsNavArchived, false);
    markWorkspaceSeen("apps", found.item);
  } catch (error) {
    if (actionSequence === workspaceNavigationActionSequence) throw error;
  }
  return true;
}

async function openWorkspaceAppLibrary(updateHistory = true) {
  const actionSequence = ++workspaceNavigationActionSequence;
  await initializeWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return false;
  webAppsNavArchived = false;
  if (!showTab("workspace-web-apps", actionSequence)) return false;
  if (updateHistory) navigateWorkspaceRoute("apps");
  await window.KernWebApps.clear();
  return true;
}

async function restoreWorkspaceRoute(route) {
  // Direct loads and copied collection URLs start without history state. Make
  // the current entry a real Workspace entry before any asynchronous restore
  // work so opening a Home detail can preserve it for browser Back.
  navigateWorkspaceRoute(route.resource, route.itemId, true);
  let opened = true;
  if (route.resource === "chat") {
    opened = route.itemId
      ? await openWorkspaceChat(route.itemId, false)
      : await openWorkspaceNewChat(false);
  } else if (route.resource === "apps") {
    opened = route.itemId
      ? await openWorkspaceWebApp(route.itemId, false)
      : await openWorkspaceAppLibrary(false);
  } else {
    opened = await openWorkspaceGlobal(route.resource, route.itemId, false);
  }
  if (opened === false) {
    recordHomeRoute("home", "", true);
    showTab("home");
    notice("That Workspace item is no longer available.", "error");
  }
}

async function setWorkspaceArchived(kind, threadId, archived) {
  const pendingKey = `${kind}:${threadId}`;
  if (workspacePendingMutations.has(pendingKey)) return;
  const actionSequence = ++workspaceNavigationActionSequence;
  const base = kind === "chat" ? "/v1/workspace/chat/threads" : "/v1/workspace/web-apps/apps";
  workspacePendingMutations.add(pendingKey);
  renderWorkspaceNavigation();
  try {
    await api("POST", `${base}/${encodeURIComponent(threadId)}/${archived ? "archive" : "unarchive"}`, {});
    await refreshWorkspaceNavigation();
    if (archived) backToHome(actionSequence);
  } finally {
    workspacePendingMutations.delete(pendingKey);
    renderWorkspaceNavigation();
  }
}

document.addEventListener("click", event => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (!target.closest(".guide-copy-button")) dismissCallbackCopyFeedback();
  // The expanded usage panel is a floating overlay; a tap anywhere outside it
  // (the pill's own tap is handled by its action) dismisses it like a menu.
  if (!target.closest(".runtime-overview")) collapseRuntimeOverview();
  const button = target.closest("button[data-action]");
  if (!button) return;
  const { action } = button.dataset;
  const runtime = button.dataset.runtime;
  const path = button.dataset.path;
  const fileType = button.dataset.fileType;
  const itemId = button.dataset.itemId;
  const actions = {
    "login": () => login(),
    "logout": () => logout(),
    "show-ios-install": () => showIPhoneInstallGuide(button),
    "close-ios-install": () => closeIPhoneInstallGuide(),
    "dismiss-ios-install": () => dismissIPhoneInstall(),
    "setup-passkey": () => setupPasskey(),
    "show-passkey-guidance": () => openPasskeyGuidance(),
    "toggle-mobile-nav": () => toggleMobileNav(),
    "close-mobile-nav": () => setMobileNavOpen(false, true),
    "new-chat": async () => {
      await openWorkspaceNewChat();
    },
    "getting-started-prompt": async () => {
      await openWorkspaceNewChat(true, STARTER_PROMPTS[button.dataset.step] || "");
    },
    "dismiss-getting-started": async () => {
      await dismissGettingStarted();
    },
    "new-web-app": async () => {
      const actionSequence = ++workspaceNavigationActionSequence;
      await mountWorkspaces();
      if (actionSequence !== workspaceNavigationActionSequence) return;
      webAppsNavArchived = false;
      if (!showTab("workspace-web-apps", actionSequence)) return;
      navigateWorkspaceRoute("apps");
      await window.KernWebApps.create();
      await refreshWorkspaceNavigation();
    },
    "open-chat": () => openWorkspaceChat(itemId),
    "open-web-app": () => openWorkspaceWebApp(itemId),
    "open-workspace-global": () => openWorkspaceGlobal(button.dataset.resource),
    "unarchive-chat": () => setWorkspaceArchived("chat", itemId, false),
    "unarchive-web-app": () => setWorkspaceArchived("web-apps", itemId, false),
    "show-chat-archive": async () => {
      const actionSequence = ++workspaceNavigationActionSequence;
      chatNavArchived = !chatNavArchived;
      await refreshWorkspaceNavigation();
      if (actionSequence !== workspaceNavigationActionSequence) return;
      backToHome(actionSequence);
    },
    "show-web-app-archive": async () => {
      const actionSequence = ++workspaceNavigationActionSequence;
      webAppsNavArchived = !webAppsNavArchived;
      await refreshWorkspaceNavigation();
      if (actionSequence !== workspaceNavigationActionSequence) return;
      backToHome(actionSequence);
    },
    "show-tab": () => button.dataset.tab === "home" ? backToHome() : showTab(button.dataset.tab),
    "open-home-view": () => openHomeView(button.dataset.view),
    "open-home-integration": () => openHomeIntegration(button.dataset.guide),
    "home-back": () => backToHome(),
    "open-provider": () => {
      collapseRuntimeOverview();
      openHomeIntegration(button.dataset.provider);
    },
    "start-login": () => startLogin(runtime),
    "reset-linked-account": () => resetLinkedAccount(button.dataset.provider),
    "complete-claude-login": () => completeClaudeLogin(),
    "refresh-provider-usage": () => refreshProviderUsage(),
    "toggle-runtime-overview": () => toggleRuntimeOverview(),
    "reboot-host": () => rebootHost(),
    "file-up": () => loadParentDirectory(),
    "file-go": () => goToFilePath(),
    "download-file": () => downloadViewedFile(),
    "open-file-path": () => openAgentPath(path, fileType),
    "load-policy": () => loadPolicy(),
    "toggle-github-repo-audit": () => toggleGithubRepoAudit(button.dataset.repoKey),
    "enable-integration": () => setIntegrationEnabled(button.dataset.integration, true),
    "disable-integration": () => setIntegrationEnabled(button.dataset.integration, false),
    "add-github-repo": () => addGithubRepo(),
    "remove-github-repo": () => removeGithubRepo(button.dataset.owner, button.dataset.repo),
    "enable-github-require-approval": () => setGithubRequireApproval(true),
    "disable-github-require-approval": () => setGithubRequireApproval(false),
    "enable-web-search": () => setProviderWebSearch(button.dataset.provider, true),
    "disable-web-search": () => setProviderWebSearch(button.dataset.provider, false),
    "connect-bedrock-credentials": () => connectBedrockCredentials(button.dataset.integration),
    "add-domain-rule": () => addDomainRule(),
    "remove-domain-rule": () => removeDomainRule(button.dataset.domain),
    "set-github-credential": () => setGithubCredential(),
    "recheck-github-audit": () => recheckGithubAudit(),
    "delete-github-credential": () => deleteGithubCredential(),
    "toggle-net-denied": () => toggleNetDeniedFilter(),
    "net-page": () => netLog.showPage(button.dataset.page).catch(() => {}),
    "agent-page": () => agentLog.showPage(button.dataset.page).catch(() => {}),
    "tool-page": () => toolLog.showPage(button.dataset.page).catch(() => {}),
    "host-diagnostic-page": () => hostDiagnosticLog.showPage(button.dataset.page).catch(() => {}),
    "toggle-host-diagnostic-filter": () => toggleHostDiagnosticFilter(),
    "approve-github-push": () => approveGithubPush(button.dataset.id),
    "reject-github-push": () => rejectGithubPush(button.dataset.id),
    "enable-tool": () => setToolEnabled(button.dataset.tool, true),
    "disable-tool": () => setToolEnabled(button.dataset.tool, false),
    "save-tool-config": () => saveToolConfig(button.dataset.tool, button.dataset.key),
    "connect-tool": () => connectTool(button.dataset.tool),
    "disconnect-tool": () => disconnectTool(button.dataset.tool),
    "decide-approval": () => decideToolApproval(button.dataset.tool, button.dataset.approvalId, button.dataset.decision),
    "copy-callback-uri": () => copyCallbackUri(button),
  };
  const handler = actions[action];
  if (!handler) return;
  try {
    Promise.resolve(handler()).catch(error => notice(error.message, "error"));
  } catch (error) {
    notice(error.message, "error");
  }
});

setUnauthorizedHandler(showLogin);
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (!$("ios-install-overlay").hidden) closeIPhoneInstallGuide();
  collapseRuntimeOverview();
  if (mobileNavOpen) setMobileNavOpen(false, true);
});
window.addEventListener("resize", () => {
  setMobileNavOpen(mobileNavOpen);
  syncIPhoneStandaloneViewport();
});
window.addEventListener("pageshow", () => {
  syncIPhoneStandaloneViewport();
  if (isIPhoneStandalone()) hideIPhoneInstallUi();
});
window.addEventListener("orientationchange", syncIPhoneStandaloneViewport);
document.addEventListener("visibilitychange", () => {
  if (document.visibilityState === "visible") syncIPhoneStandaloneViewport();
});
window.addEventListener("popstate", event => {
  const workspaceRoute = workspaceRouteFromLocation();
  if (workspaceRoute) {
    void restoreWorkspaceRoute(workspaceRoute).catch(error => notice(error.message, "error"));
    return;
  }
  const route = event.state?.kernHomeRoute;
  if (route && route !== "home" && homeDetailTabs.has(route)) {
    openHomeView(route, event.state.guideId || "", false);
  } else {
    showTab("home");
  }
});
bindIPhoneStandaloneSidebarSwipe();
$("github-credential-mode").addEventListener("change", toggleGithubCredentialMode);
$("password").addEventListener("keydown", event => { if (event.key === "Enter") login(); });
$("file-path").addEventListener("keydown", event => { if (event.key === "Enter") goToFilePath(); });
start();
