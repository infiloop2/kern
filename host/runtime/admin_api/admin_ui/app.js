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
  refreshProviderUsage, rebootHost, startLogin, toggleRuntimeOverview,
} from "./health.js";
import {
  ensureFilesLoaded, goToFilePath, loadParentDirectory, openAgentPath,
  refreshFiles,
} from "./files.js";
import { refreshAgentProcesses } from "./processes.js";
import { agentLog, hostDiagnosticLog, netLog, toolLog, toggleHostDiagnosticFilter, toggleNetDeniedFilter } from "./logs.js";
import {
  addDomainRule, addGithubRepo, approveGithubPush, deleteGithubCredential,
  loadPolicy, recheckGithubAudit, rejectGithubPush, removeDomainRule,
  removeGithubRepo, resetLinkedAccount, connectBedrockCredentials, setClaudeWebSearch, setGithubCredential, setGithubRequireApproval,
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
  return window.matchMedia("(display-mode: standalone)").matches
    || window.navigator.standalone === true;
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
const staticTabs = ["home", "processes", "agent-log", "files", "network", "net-log", "tool-log", "host-diagnostics"];
const homeDetailTabs = new Set(staticTabs.filter(name => name !== "home"));
const MOBILE_NAV_QUERY = "(max-width: 860px)";
let mobileNavOpen = false;
let uploadPickerOpen = false;
let nextUploadSelectionId = 1;
const APP_UPLOAD_SELECTION_LIMIT = 10;
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
window.KernWorkspaceRoots = {};

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

function resetPageScroll() {
  window.scrollTo({ top: 0, left: 0, behavior: "instant" });
}

let workspaceViewportRecovery = 0;
function recoverWorkspaceViewport() {
  if (!document.body.classList.contains("viewport-panel-open")
      || !window.matchMedia(MOBILE_NAV_QUERY).matches) return;
  cancelAnimationFrame(workspaceViewportRecovery);
  workspaceViewportRecovery = requestAnimationFrame(() => {
    workspaceViewportRecovery = requestAnimationFrame(() => {
      /* Mobile Safari may pan the layout viewport to expose a focused field.
         It does not always restore that pan when Send clears the field or the
         keyboard closes. The workspaces own the visual viewport, so the page
         itself must remain anchored while their internal scrollers move. */
      resetPageScroll();
      document.documentElement.scrollTop = 0;
      document.body.scrollTop = 0;
    });
  });
}

document.addEventListener("focusout", event => {
  const target = event.composedPath()[0];
  if (!(target instanceof HTMLInputElement) && !(target instanceof HTMLTextAreaElement)) return;
  recoverWorkspaceViewport();
  setTimeout(recoverWorkspaceViewport, 180);
}, true);
if (window.visualViewport) {
  window.visualViewport.addEventListener("resize", recoverWorkspaceViewport, { passive: true });
  window.visualViewport.addEventListener("scroll", recoverWorkspaceViewport, { passive: true });
}

function showLogin() {
  setMobileNavOpen(false);
  hideIPhoneInstallUi();
  document.body.classList.remove("viewport-panel-open");
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
  if (viewportPanelOpen || name === "home" || homeDetailTabs.has(name)) resetPageScroll();
  activeTabRefresh = refreshVisibleTab(name).catch(() => {});
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

function openHomeView(view, guideId = "", updateHistory = true) {
  if (!homeDetailTabs.has(view)) return;
  if (view === "network") {
    selectToolDetail(guideId);
    selectIntegrationDetail(guideId);
    openConnectionGuide(guideId);
  }
  if (updateHistory) {
    if (!history.state?.kernHomeRoute) recordHomeRoute("home", "", true);
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
  mountWorkspaces().then(refreshWorkspaceNavigation).catch(error => notice(error.message, "error"));
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
    if (!history.state?.kernHomeRoute) {
      const route = homeRouteFromLocation();
      recordHomeRoute(route.view, route.guideId, true);
    }
    const route = history.state?.kernHomeRoute;
    if (route && route !== "home" && homeDetailTabs.has(route)) {
      openHomeView(route, history.state.guideId || "", false);
    } else {
      showTab("home");
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

async function openWorkspaceGlobal(resource) {
  const actionSequence = ++workspaceNavigationActionSequence;
  await mountWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return;
  if (!showTab("workspace-global", actionSequence)) return;
  await window.KernWorkspaceGlobal.open(resource);
  renderWorkspaceNavigation();
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
    button.append(primary);
    if (kind === "chat") {
      const settings = [runtimeLabel(item.agent_runtime), item.model, item.effort]
        .filter(Boolean)
        .join(" · ");
      const meta = document.createElement("span");
      meta.className = "workspace-nav-meta";
      meta.textContent = settings;
      button.append(meta);
      button.title = settings ? `${item.name || itemId}\n${settings}` : item.name || itemId;
    } else {
      button.title = item.name || itemId;
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

async function openWorkspaceChat(threadId) {
  if (workspacePendingMutations.has(`chat:${threadId}`)) return;
  const actionSequence = ++workspaceNavigationActionSequence;
  await mountWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return;
  const thread = chatNavItems.find(item => item.thread_id === threadId);
  if (!thread) return;
  if (!showTab("workspace-chat", actionSequence)) return;
  try {
    await window.KernChat.openThread(thread);
  } catch (error) {
    if (actionSequence === workspaceNavigationActionSequence) throw error;
  }
}

async function openWorkspaceWebApp(appId) {
  if (workspacePendingMutations.has(`web-apps:${appId}`)) return;
  const actionSequence = ++workspaceNavigationActionSequence;
  await mountWorkspaces();
  if (actionSequence !== workspaceNavigationActionSequence) return;
  const app = webAppNavItems.find(item => item.app_id === appId);
  if (!app) return;
  if (!showTab("workspace-web-apps", actionSequence)) return;
  try {
    await window.KernWebApps.open(app, webAppsNavArchived);
  } catch (error) {
    if (actionSequence === workspaceNavigationActionSequence) throw error;
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
      const actionSequence = ++workspaceNavigationActionSequence;
      await mountWorkspaces();
      if (actionSequence !== workspaceNavigationActionSequence) return;
      chatNavArchived = false;
      if (!showTab("workspace-chat", actionSequence)) return;
      window.KernChat.newThread();
      await refreshWorkspaceNavigation();
    },
    "new-web-app": async () => {
      const actionSequence = ++workspaceNavigationActionSequence;
      await mountWorkspaces();
      if (actionSequence !== workspaceNavigationActionSequence) return;
      webAppsNavArchived = false;
      if (!showTab("workspace-web-apps", actionSequence)) return;
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
    "open-file-path": () => openAgentPath(path, fileType),
    "load-policy": () => loadPolicy(),
    "toggle-github-repo-audit": () => toggleGithubRepoAudit(button.dataset.repoKey),
    "enable-integration": () => setIntegrationEnabled(button.dataset.integration, true),
    "disable-integration": () => setIntegrationEnabled(button.dataset.integration, false),
    "add-github-repo": () => addGithubRepo(),
    "remove-github-repo": () => removeGithubRepo(button.dataset.owner, button.dataset.repo),
    "enable-github-require-approval": () => setGithubRequireApproval(true),
    "disable-github-require-approval": () => setGithubRequireApproval(false),
    "enable-claude-web-search": () => setClaudeWebSearch(true),
    "disable-claude-web-search": () => setClaudeWebSearch(false),
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
window.addEventListener("resize", () => setMobileNavOpen(mobileNavOpen));
window.addEventListener("pageshow", () => {
  if (isIPhoneStandalone()) hideIPhoneInstallUi();
});
window.addEventListener("popstate", event => {
  const route = event.state?.kernHomeRoute;
  if (route && route !== "home" && homeDetailTabs.has(route)) {
    openHomeView(route, event.state.guideId || "", false);
  } else {
    showTab("home");
  }
});
$("github-credential-mode").addEventListener("change", toggleGithubCredentialMode);
$("password").addEventListener("keydown", event => { if (event.key === "Enter") login(); });
$("file-path").addEventListener("keydown", event => { if (event.key === "Enter") goToFilePath(); });
start();
