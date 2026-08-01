// Entry module: session lifecycle (login, logout), tab switching, the
// 5-second refresh tick, and the one delegated click dispatcher that maps
// data-action buttons to feature handlers. Feature code lives in the sibling
// modules; this file is the only place that wires them together.

import {
  api, apiUpload, login as apiLogin, logout as apiLogout, setUnauthorizedHandler,
} from "./api.js";
import { $, notice } from "./helpers.js";
import {
  collapseRuntimeOverview, completeClaudeLogin, refreshHealth, refreshProviderAccounts,
  refreshProviderUsage, rebootHost, startLogin, toggleRuntimeOverview,
} from "./health.js";
import {
  loadEarlierThreadEvents, loadThreads, refreshSelectedThread,
  renderThreadHistory, showThread,
} from "./threads.js";
import {
  ensureFilesLoaded, goToFilePath, loadParentDirectory, openAgentPath,
  refreshFiles,
} from "./files.js";
import { refreshAgentProcesses } from "./processes.js";
import { agentLog, hostErrorLog, netLog, toolLog, toggleNetDeniedFilter } from "./logs.js";
import {
  addDomainRule, addGithubRepo, approveGithubPush, closeIntegrationInfo, deleteGithubCredential,
  loadPolicy, openProvider, recheckGithubAudit, rejectGithubPush, removeDomainRule,
  removeGithubRepo, resetLinkedAccount, connectBedrockCredentials, setClaudeWebSearch, setGithubCredential, setGithubRequireApproval,
  setIntegrationEnabled, positionIntegrationInfo, refreshPendingGithubPushes, toggleGithubCredentialMode, toggleIntegrationExpansion,
  toggleCustomDomainAccess, toggleGithubRepoAudit, toggleIntegrationInfo,
} from "./network.js";
import {
  completeToolConnect, connectTool, decideToolApproval, disconnectTool,
  refreshExpandedToolApprovals, refreshTools, saveToolConfig, setToolEnabled,
  toggleToolExpansion, toggleToolInfo,
} from "./tools.js";
import {
  copyCallbackUri, dismissCallbackCopyFeedback, openConnectionGuide, refreshConnectionGuide,
} from "./connection_guide.js";
import {
  finishPasskeyLogin, refreshLoginPasskeyStatus, refreshPasskeySetup,
  setupPasskey, showPasskeyGuidance,
} from "./passkeys.js";

let activeTab = "home";
let installedApps = [];
const appFrames = new Map();
const staticTabs = ["home", "agent", "processes", "agent-log", "files", "network", "connection-guide", "net-log", "tool-log", "host-errors"];
const HERO_APP_ID = "agent_chat";
const HERO_CTA = "Begin chat";
const MOBILE_NAV_QUERY = "(max-width: 860px)";
let mobileNavOpen = false;
let uploadPickerOpen = false;
let nextUploadSelectionId = 1;
const APP_UPLOAD_SELECTION_LIMIT = 10;
const pendingAppUploads = new Map();
let betaAppsExpanded = false;

function showLoginError(message) {
  const element = $("login-error");
  element.textContent = message;
  element.hidden = false;
}

function clearLegacyPasswordCookie() {
  // Pre-0.44 UIs stored the cleartext admin password in this JavaScript-readable
  // cookie. Expire it on every load so an upgraded browser never keeps the
  // password readable or keeps sending it to the origin until it ages out.
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

function showLogin() {
  setMobileNavOpen(false);
  document.body.classList.remove("connection-guide-open");
  document.body.classList.remove("app-tab-open");
  document.body.classList.remove("viewport-panel-open");
  document.body.classList.remove("host-fullscreen-app-open");
  $("login").hidden = false;
  $("app").hidden = true;
  $("logout-button").hidden = true;
  $("agent-name").hidden = true;
  $("runtime-overview").hidden = true;
  $("passkey-status-control").hidden = true;
  $("upgrade-notice").hidden = true;
  $("mobile-nav-toggle").hidden = true;
  refreshLoginPasskeyStatus();
}

function showTab(name) {
  const closeDrawer = mobileNavOpen;
  activeTab = name;
  const connectionGuideOpen = name === "connection-guide";
  const appTabOpen = name.startsWith("app:");
  const fullscreenAppOpen = appTabOpen && installedApps.some(
    app => name === `app:${app.id}` && app.ui?.host_fullscreen === true,
  );
  const viewportPanelOpen = connectionGuideOpen || appTabOpen;
  if (viewportPanelOpen) window.scrollTo(0, 0);
  document.body.classList.toggle("connection-guide-open", connectionGuideOpen);
  document.body.classList.toggle("app-tab-open", appTabOpen);
  document.body.classList.toggle("viewport-panel-open", viewportPanelOpen);
  document.body.classList.toggle("host-fullscreen-app-open", fullscreenAppOpen);
  for (const tabName of staticTabs) {
    $(`tab-${tabName}`).classList.toggle("active-tab", tabName === name);
    $(`panel-${tabName}`).hidden = tabName !== name;
  }
  for (const app of installedApps) {
    const selected = name === `app:${app.id}`;
    $(`tab-app-${app.id}`)?.classList.toggle("active-tab", selected);
    const panel = $(`panel-app-${app.id}`);
    if (panel) panel.hidden = !selected;
    if (selected) loadAppFrame(app);
  }
  setMobileNavOpen(false, closeDrawer);
  refreshVisibleTab(name).catch(() => {});
}

function openPasskeyGuidance() {
  const control = $("passkey-status-control");
  if (control.dataset.configured === "true") {
    showPasskeyGuidance();
    return;
  }
  showTab("home");
  showPasskeyGuidance();
  setupPasskey();
}

async function refreshVisibleTab(name) {
  if (name === "agent-log") {
    await agentLog.showFirstPage();
  } else if (name === "net-log") {
    await netLog.showFirstPage();
  } else if (name === "tool-log") {
    await toolLog.showFirstPage();
  } else if (name === "host-errors") {
    await hostErrorLog.showFirstPage();
  } else if (name === "processes") {
    await refreshAgentProcesses();
  } else if (name === "files") {
    await ensureFilesLoaded();
  } else if (name === "network") {
    // Tool rows hold config inputs, so they refresh on tab entry and after
    // actions only, never on the 5-second tick (that would wipe half-typed
    // values). Expanded approvals carry no inputs and also refresh on the
    // tick below.
    await refreshTools();
    await refreshExpandedToolApprovals();
  } else if (name === "connection-guide") {
    await refreshConnectionGuide();
  }
}

async function tick() {
  await refreshOrSkip(refreshHealth);
  await refreshOrSkip(refreshProviderAccounts);
  await refreshOrSkip(loadThreads);
  await refreshOrSkip(refreshSelectedThread);
  if (activeTab === "agent-log" && agentLog.page === 1) await refreshOrSkip(() => agentLog.showFirstPage());
  if (activeTab === "net-log" && netLog.page === 1) await refreshOrSkip(() => netLog.showFirstPage());
  if (activeTab === "network") await refreshOrSkip(refreshPendingGithubPushes);
  if (activeTab === "processes") await refreshOrSkip(refreshAgentProcesses);
  if (activeTab === "files") await refreshOrSkip(refreshFiles);
  if (activeTab === "network") await refreshOrSkip(refreshExpandedToolApprovals);
  if (activeTab === "tool-log" && toolLog.page === 1) await refreshOrSkip(() => toolLog.showFirstPage());
  if (activeTab === "host-errors" && hostErrorLog.page === 1) await refreshOrSkip(() => hostErrorLog.showFirstPage());
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
  loadApps().catch(error => notice(error.message, "error"));
  refreshPasskeySetup();
  renderThreadHistory();
  loadPolicy().catch(() => {});
  // The provider redirects a tool OAuth connect back to /oauth/callback; land
  // on the network tab and finish the token exchange the operator started.
  if (location.pathname === "/oauth/callback") {
    showTab("network");
    completeToolConnect().catch(error => notice(error.message, "error"));
  }
  // Guard the recurring tick so a re-login within the same page load (the login
  // screen never reloads on success) cannot stack a second interval.
  if (appStarted) return;
  appStarted = true;
  tick();
  setInterval(tick, 5000);
}

async function loadApps() {
  const response = await api("GET", "/v1/apps");
  installedApps = response.apps || [];
  renderAppTabs();
}

function renderAppTabs() {
  const stableContainer = $("stable-app-tabs");
  const betaContainer = $("beta-app-tabs");
  stableContainer.innerHTML = "";
  betaContainer.innerHTML = "";
  document.querySelectorAll(".app-tab-panel").forEach(panel => panel.remove());
  appFrames.clear();
  // Agent Chat gets the Home navigator, but remains an ordinary stable app in
  // the sidebar.
  const heroApp = installedApps.find(app => app.id === HERO_APP_ID) || null;
  renderHomeHero(heroApp);
  const stableApps = installedApps.filter(app => app.release_stage !== "beta");
  const betaApps = installedApps.filter(app => app.release_stage === "beta");
  $("sidebar-stable-apps").hidden = !stableApps.length;
  $("sidebar-apps").hidden = !betaApps.length;
  betaAppsExpanded = false;
  $("sidebar-apps-toggle").setAttribute("aria-expanded", "false");
  betaContainer.hidden = true;
  const main = document.querySelector("main");
  for (const app of installedApps) {
    const button = document.createElement("button");
    button.id = `tab-app-${app.id}`;
    button.className = "tab-button";
    button.dataset.action = "show-tab";
    button.dataset.tab = `app:${app.id}`;
    button.innerHTML = `${appIconSvg()}<span></span>`;
    button.querySelector("span").textContent = app.title || app.id;
    (app.release_stage === "beta" ? betaContainer : stableContainer).appendChild(button);

    const panel = document.createElement("div");
    panel.id = `panel-app-${app.id}`;
    panel.className = "tab-panel app-tab-panel";
    panel.hidden = activeTab !== `app:${app.id}`;
    const section = document.createElement("section");
    section.className = "app-frame-section";
    panel.appendChild(section);
    if (app.ui.host_fullscreen === true) {
      const exit = document.createElement("button");
      exit.className = "host-app-exit";
      exit.dataset.action = "show-tab";
      exit.dataset.tab = "home";
      exit.setAttribute("aria-label", "Back to host");
      exit.innerHTML = `
        <svg viewBox="0 0 20 20" aria-hidden="true">
          <path d="m5 5 10 10M15 5 5 15" fill="none" stroke="currentColor"
            stroke-width="1.7" stroke-linecap="round"/>
        </svg>
        <span>Back to host</span>`;
      panel.appendChild(exit);
    }
    main.appendChild(panel);
    if (activeTab === `app:${app.id}`) loadAppFrame(app);
  }
  // Login can resume within the same page after showLogin cleared viewport
  // classes. Reapply the selected app's host presentation once its manifest
  // is available again.
  if (activeTab.startsWith("app:")) showTab(activeTab);
}

function toggleBetaApps() {
  betaAppsExpanded = !betaAppsExpanded;
  $("sidebar-apps-toggle").setAttribute("aria-expanded", String(betaAppsExpanded));
  $("beta-app-tabs").hidden = !betaAppsExpanded;
}

function loadAppFrame(app) {
  if (appFrames.has(app.id)) return;
  const section = $(`panel-app-${app.id}`)?.querySelector(".app-frame-section");
  if (!section) return;
  const iframe = document.createElement("iframe");
  iframe.className = "app-frame";
  iframe.title = app.title || app.id;
  iframe.setAttribute("sandbox", app.ui.sandbox.join(" "));
  iframe.src = app.ui.iframe_src;
  section.appendChild(iframe);
  appFrames.set(app.id, iframe);
}

function renderHomeHero(heroApp) {
  const hero = $("home-hero");
  hero.hidden = !heroApp;
  hero.innerHTML = "";
  if (!heroApp) return;
  const card = document.createElement("section");
  card.className = "home-hero-card";
  card.innerHTML = `
    <div class="home-hero-copy">
      <span class="home-hero-icon">${chatIconSvg()}</span>
      <h1></h1>
    </div>
    <button class="home-hero-cta" data-action="show-tab"></button>`;
  card.querySelector("h1").textContent = heroApp.title || heroApp.id;
  const cta = card.querySelector(".home-hero-cta");
  cta.dataset.tab = `app:${heroApp.id}`;
  cta.textContent = HERO_CTA;
  hero.appendChild(card);
}

function appIconSvg() {
  return `<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 5.5A1.5 1.5 0 0 1 5.5 4h9A1.5 1.5 0 0 1 16 5.5v9a1.5 1.5 0 0 1-1.5 1.5h-9A1.5 1.5 0 0 1 4 14.5v-9Z" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M7 8h6M7 11h4" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
}

function chatIconSvg() {
  return `<svg width="19" height="19" viewBox="0 0 20 20" aria-hidden="true"><path d="M4 4.5h12A1.5 1.5 0 0 1 17.5 6v6a1.5 1.5 0 0 1-1.5 1.5H9.4L6 16.5v-3H4A1.5 1.5 0 0 1 2.5 12V6A1.5 1.5 0 0 1 4 4.5Z" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linejoin="round"/><path d="M6.5 8h7M6.5 10.6h4.6" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>`;
}

window.addEventListener("message", event => {
  const message = event.data;
  if (!message || ![
    "kern-app-api",
    "kern-app-copy-text",
    "kern-app-open-file",
    "kern-app-upload-file",
  ].includes(message.type)) return;
  const app = installedApps.find(candidate => appFrames.get(candidate.id)?.contentWindow === event.source);
  if (!app) return;
  if (message.type === "kern-app-open-file") {
    const path = typeof message.path === "string" ? message.path : "";
    if (!path.startsWith("/") || path.split("/").includes("..")) return;
    showTab("files");
    openAgentPath(path, "file").catch(error => notice(error.message, true));
    return;
  }
  if (message.type === "kern-app-copy-text") {
    handleAppCopyTextMessage(event.source, message).catch(error => {
      event.source.postMessage({
        type: "kern-app-copy-text-result",
        request_id: String(message.request_id || ""),
        ok: false,
        error: error.message,
      }, "*");
    });
    return;
  }
  if (message.type === "kern-app-upload-file") {
    handleAppUploadMessage(app, event.source, message).catch(error => {
      event.source.postMessage({
        type: "kern-app-upload-file-result",
        request_id: String(message.request_id || ""),
        ok: false,
        error: error.message,
      }, "*");
    });
    return;
  }
  handleAppApiMessage(app, event.source, message).catch(error => {
    event.source.postMessage({
      type: "kern-app-api-result",
      request_id: message.request_id,
      ok: false,
      error: error.message,
    }, "*");
  });
});

async function handleAppCopyTextMessage(source, message) {
  const text = typeof message.text === "string" ? message.text : "";
  if (!text || new TextEncoder().encode(text).length > 2 * 1024 * 1024) {
    throw new Error("app clipboard text must be between 1 byte and 2 MiB");
  }
  let copied = false;
  if (navigator.clipboard && window.isSecureContext) {
    try {
      await navigator.clipboard.writeText(text);
      copied = true;
    } catch (_error) {
      // Fall through to the selection-based copy path for browsers that
      // expose the API but deny it in this embedding context.
    }
  }
  if (!copied) {
    const area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "fixed";
    area.style.opacity = "0";
    document.body.append(area);
    area.select();
    copied = document.execCommand("copy");
    area.remove();
    if (!copied) throw new Error("copy failed");
  }
  source.postMessage({
    type: "kern-app-copy-text-result",
    request_id: String(message.request_id || ""),
    ok: true,
    body: { copied: true },
  }, "*");
}

async function handleAppUploadMessage(app, source, message) {
  const action = message.action;
  if (action === "select") {
    const maximum = message.max_files === undefined ? APP_UPLOAD_SELECTION_LIMIT : message.max_files;
    if (!Number.isInteger(maximum) || maximum < 1 || maximum > APP_UPLOAD_SELECTION_LIMIT) {
      throw new Error(`an app can select between 1 and ${APP_UPLOAD_SELECTION_LIMIT} files`);
    }
    if (uploadPickerOpen) throw new Error("another file selection is already open");
    uploadPickerOpen = true;
    try {
      const files = await chooseUploadFiles();
      if (files === null) {
        source.postMessage({
          type: "kern-app-upload-file-result",
          request_id: String(message.request_id || ""),
          ok: true,
          cancelled: true,
        }, "*");
        return;
      }
      const appSelections = appUploadSelections(app.id);
      if (files.length > maximum || appSelections.size + files.length > APP_UPLOAD_SELECTION_LIMIT) {
        throw new Error(`You can attach up to ${APP_UPLOAD_SELECTION_LIMIT} files.`);
      }
      const selections = files.map(file => {
        const selectionId = String(nextUploadSelectionId++);
        appSelections.set(selectionId, file);
        return {
          selection_id: selectionId,
          original_name: file.name,
          size_bytes: file.size,
        };
      });
      source.postMessage({
        type: "kern-app-upload-file-result",
        request_id: String(message.request_id || ""),
        ok: true,
        body: { selections },
      }, "*");
    } finally {
      uploadPickerOpen = false;
    }
    return;
  }

  const selectionId = String(message.selection_id || "");
  const appSelections = pendingAppUploads.get(app.id);
  const selected = appSelections && appSelections.get(selectionId);
  if (!selected) {
    throw new Error("file selection is no longer available");
  }
  if (action === "discard") {
    removeAppUploadSelection(app.id, selectionId);
    source.postMessage({
      type: "kern-app-upload-file-result",
      request_id: String(message.request_id || ""),
      ok: true,
      body: { discarded: true },
    }, "*");
    return;
  }
  if (action !== "upload") throw new Error("file upload action is not allowed");

  // Consume the in-memory selection before starting I/O so duplicate bridge
  // requests cannot publish the same local file twice. Restore it after a
  // failed upload while keeping the per-app selection bound.
  removeAppUploadSelection(app.id, selectionId);
  let body;
  try {
    body = await apiUpload(selected);
  } catch (error) {
    const current = appUploadSelections(app.id);
    if (current.size < APP_UPLOAD_SELECTION_LIMIT) current.set(selectionId, selected);
    throw error;
  }
  source.postMessage({
    type: "kern-app-upload-file-result",
    request_id: String(message.request_id || ""),
    ok: true,
    body,
  }, "*");
}

function appUploadSelections(appId) {
  let selections = pendingAppUploads.get(appId);
  if (!selections) {
    selections = new Map();
    pendingAppUploads.set(appId, selections);
  }
  return selections;
}

function removeAppUploadSelection(appId, selectionId) {
  const selections = pendingAppUploads.get(appId);
  if (!selections) return;
  selections.delete(selectionId);
  if (!selections.size) pendingAppUploads.delete(appId);
}

function chooseUploadFiles() {
  return new Promise(resolve => {
    // A file picker requires transient user activation. An app can post this
    // message at any time, so settle a non-user-initiated request instead of
    // leaving the global picker lock held when the browser ignores click().
    if (navigator.userActivation && !navigator.userActivation.isActive) {
      resolve(null);
      return;
    }
    const input = document.createElement("input");
    input.type = "file";
    input.multiple = true;
    input.className = "host-file-picker";
    document.body.appendChild(input);
    let settled = false;
    const finish = files => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(files);
    };
    const currentFiles = () => {
      const files = input.files ? Array.from(input.files) : [];
      return files.length ? files : null;
    };
    input.addEventListener("change", () => finish(currentFiles()), { once: true });
    input.addEventListener("cancel", () => finish(null), { once: true });
    // Safari versions without the cancel event return focus to the page when
    // the picker closes. Give a selected file's change event one tick to run.
    window.addEventListener(
      "focus",
      () => setTimeout(() => finish(currentFiles()), 0),
      { once: true },
    );
    try {
      input.click();
    } catch (_error) {
      finish(null);
    }
  });
}

async function handleAppApiMessage(app, source, message) {
  // Friendly pre-check only: the admin API enforces the bridge scope
  // server-side (a bridge-tagged request outside the app's own API is 403).
  const route = app && app.backend && app.backend.api_route;
  if (
    !["GET", "POST", "PUT", "DELETE"].includes(message.method) ||
    typeof message.path !== "string" ||
    typeof route !== "string" ||
    !message.path.startsWith(route)
  ) {
    throw new Error("app API route is not allowed");
  }
  const body = await api(message.method, message.path, message.body, { "X-Kern-App-Bridge": app.id });
  source.postMessage({
    type: "kern-app-api-result",
    request_id: String(message.request_id || ""),
    ok: true,
    body,
  }, "*");
}

document.addEventListener("click", event => {
  const target = event.target;
  if (!(target instanceof Element)) return;
  if (!target.closest(".info-button, #preset-info-popover")) closeIntegrationInfo();
  if (!target.closest(".guide-copy-button")) dismissCallbackCopyFeedback();
  // The expanded usage panel is a floating overlay; a tap anywhere outside it
  // (the pill's own tap is handled by its action) dismisses it like a menu.
  if (!target.closest(".runtime-overview")) collapseRuntimeOverview();
  const button = target.closest("button[data-action]");
  if (!button) return;
  const { action } = button.dataset;
  const threadId = button.dataset.threadId;
  const runtime = button.dataset.runtime;
  const path = button.dataset.path;
  const fileType = button.dataset.fileType;
  const actions = {
    "login": () => login(),
    "logout": () => logout(),
    "setup-passkey": () => setupPasskey(),
    "show-passkey-guidance": () => openPasskeyGuidance(),
    "toggle-mobile-nav": () => toggleMobileNav(),
    "close-mobile-nav": () => setMobileNavOpen(false, true),
    "toggle-beta-apps": () => toggleBetaApps(),
    "show-tab": () => showTab(button.dataset.tab),
    "open-provider": () => { collapseRuntimeOverview(); showTab("network"); openProvider(button.dataset.provider); },
    "start-login": () => startLogin(runtime),
    "reset-linked-account": () => resetLinkedAccount(button.dataset.provider),
    "complete-claude-login": () => completeClaudeLogin(),
    "refresh-provider-usage": () => refreshProviderUsage(),
    "toggle-runtime-overview": () => toggleRuntimeOverview(),
    "reboot-host": () => rebootHost(),
    "show-thread": () => showThread(threadId, runtime),
    "load-earlier-thread-events": () => loadEarlierThreadEvents(),
    "file-up": () => loadParentDirectory(),
    "file-go": () => goToFilePath(),
    "open-file-path": () => openAgentPath(path, fileType),
    "load-policy": () => loadPolicy(),
    "toggle-integration-info": () => toggleIntegrationInfo(button.dataset.info, button),
    "toggle-integration-expansion": () => toggleIntegrationExpansion(button.dataset.integration),
    "toggle-custom-domain-access": () => toggleCustomDomainAccess(),
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
    "host-error-page": () => hostErrorLog.showPage(button.dataset.page).catch(() => {}),
    "approve-github-push": () => approveGithubPush(button.dataset.id),
    "reject-github-push": () => rejectGithubPush(button.dataset.id),
    "enable-tool": () => setToolEnabled(button.dataset.tool, true),
    "disable-tool": () => setToolEnabled(button.dataset.tool, false),
    "save-tool-config": () => saveToolConfig(button.dataset.tool, button.dataset.key),
    "connect-tool": () => connectTool(button.dataset.tool),
    "disconnect-tool": () => disconnectTool(button.dataset.tool),
    "toggle-tool-info": () => toggleToolInfo(button.dataset.tool, button),
    "toggle-tool-expansion": () => toggleToolExpansion(button.dataset.tool),
    "decide-approval": () => decideToolApproval(button.dataset.tool, button.dataset.approvalId, button.dataset.decision),
    "open-connection-guide": () => {
      closeIntegrationInfo();
      openConnectionGuide(button.dataset.guide);
      showTab("connection-guide");
    },
    "jump-connection-guide": () => openConnectionGuide(button.dataset.guide),
    "copy-callback-uri": () => copyCallbackUri(button),
  };
  const handler = actions[action];
  if (handler) handler();
});

setUnauthorizedHandler(showLogin);
document.addEventListener("keydown", event => {
  if (event.key !== "Escape") return;
  if (document.body.classList.contains("host-fullscreen-app-open")) {
    showTab("home");
    return;
  }
  closeIntegrationInfo();
  collapseRuntimeOverview();
  if (mobileNavOpen) setMobileNavOpen(false, true);
});
window.addEventListener("resize", () => {
  positionIntegrationInfo();
  setMobileNavOpen(mobileNavOpen);
});
document.addEventListener("scroll", positionIntegrationInfo, true);
$("github-credential-mode").addEventListener("change", toggleGithubCredentialMode);
$("connection-guide-select").addEventListener("change", event => openConnectionGuide(event.target.value));
$("password").addEventListener("keydown", event => { if (event.key === "Enter") login(); });
$("file-path").addEventListener("keydown", event => { if (event.key === "Enter") goToFilePath(); });
start();
