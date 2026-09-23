// Backend access: the one fetch wrapper every module calls. Authentication is
// an HttpOnly session cookie the browser sends automatically after /v1/login;
// this code never reads or holds the admin password. Every request carries the
// CSRF header so the server accepts the cookie (a cross-site page cannot set
// it). app.js registers what happens on a 401 (show the login screen).

const CSRF_HEADER = "X-Kern-Csrf";
const SESSION_ACTIVITY_HEADER = "X-Kern-Session-Activity";
const RECENT_OPERATOR_ACTIVITY_MS = 60 * 1000;

// A page load is itself operator activity. Thereafter only real UI interaction
// advances this timestamp; the five-second refresh loop never does. The server
// remains authoritative for both idle and absolute expiry—this marker merely
// tells it which already-authenticated requests may refresh the idle clock.
let lastOperatorActivityAt = Date.now();

export function markSessionActivity(event) {
  // Programmatically dispatched DOM events are not operator activity. Calls
  // without an event come only from the host's trusted Workspace UI path.
  if (event && !event.isTrusted) return;
  lastOperatorActivityAt = Date.now();
}

for (const eventName of ["click", "keydown", "pointerdown", "touchstart", "wheel"]) {
  window.addEventListener(eventName, markSessionActivity, { capture: true, passive: true });
}

function authenticatedHeaders(extraHeaders) {
  const headers = { [CSRF_HEADER]: "1" };
  if (Date.now() - lastOperatorActivityAt <= RECENT_OPERATOR_ACTIVITY_MS) {
    headers[SESSION_ACTIVITY_HEADER] = "1";
  }
  for (const [name, value] of Object.entries(extraHeaders || {})) headers[name] = value;
  return headers;
}

let unauthorizedHandler = () => {};

export function setUnauthorizedHandler(handler) {
  unauthorizedHandler = handler;
}

// Shared by Home and Workspace callers. A failed action is never replayed.
let unavailableUntil = 0;
let unavailableDelay = 0;
let unavailableGeneration = 0;
let unavailableStatus = 503;
let availabilityHandler = () => {};
const BUSY_MESSAGE = "Kern is busy or temporarily unavailable. Refreshes will resume shortly.";
const READ_DEADLINE_MS = 30000;

export function setAvailabilityHandler(handler) { availabilityHandler = handler; }
export function isOverloadCoolingDown() { return Date.now() < unavailableUntil; }

function unavailableError() {
  const error = new Error(BUSY_MESSAGE);
  error.status = unavailableStatus;
  error.code = "host_unavailable";
  return error;
}

function markUnavailable(response) {
  unavailableStatus = response?.status || 0;
  // Concurrent failures belong to one cooldown, rather than multiplying it.
  if (!isOverloadCoolingDown()) {
    const retryHeader = response?.headers.get("Retry-After");
    const retrySeconds = Number(retryHeader);
    const retryMs = Number.isFinite(retrySeconds) && retrySeconds > 0 ? retrySeconds * 1000 : 0;
    unavailableDelay = Math.min(60000, Math.max(5000, unavailableDelay * 2, retryMs));
    unavailableUntil = Date.now() + unavailableDelay;
    unavailableGeneration += 1;
    availabilityHandler(BUSY_MESSAGE);
  }
}

async function availableFetch(path, options, { bypassCooldown = false } = {}) {
  if (!bypassCooldown && isOverloadCoolingDown()) throw unavailableError();
  const generation = unavailableGeneration;
  let response;
  try {
    response = await fetch(path, options);
  } catch (error) {
    if (error.name !== "AbortError") {
      markUnavailable(null);
      throw unavailableError();
    }
    throw error;
  }
  if ([502, 503, 504].includes(response.status)) {
    let detail = null;
    if ((response.headers.get("Content-Type") || "").includes("application/json")) {
      try { detail = await response.clone().json(); } catch (_) {}
    }
    // A structured endpoint failure (for example an unavailable integration)
    // must retain its message and must not pause unrelated dashboard sections.
    if (typeof detail?.error?.message === "string" && detail.error.code !== "host_busy") {
      return response;
    }
    markUnavailable(response);
    if (response.body) void response.body.cancel().catch(() => {});
    throw unavailableError();
  }
  // An older in-flight success must not erase a newly observed outage.
  if (generation === unavailableGeneration && unavailableDelay) {
    unavailableDelay = 0;
    unavailableUntil = 0;
    availabilityHandler("");
  }
  return response;
}

// Bound dashboard reads through body parsing as well as connection setup.
// A stalled read must release the refresh tick so a later one can recover.
async function withReadDeadline(work) {
  const controller = new AbortController();
  let expired = false;
  const timer = setTimeout(() => {
    expired = true;
    controller.abort();
  }, READ_DEADLINE_MS);
  try {
    return await work(controller.signal);
  } catch (error) {
    if (expired) {
      markUnavailable(null);
      throw unavailableError();
    }
    throw error;
  } finally {
    clearTimeout(timer);
  }
}

export function workspaceHtml(path) {
  return withReadDeadline(async signal => {
    const response = await availableFetch(path, { credentials: "same-origin", signal });
    if (!response.ok) throw new Error(`Could not load ${path}`);
    return response.text();
  });
}

// POST the password to mint a session cookie. Returns the raw Response so the
// caller can distinguish a wrong password (401) from a throttled attempt (429).
export function login(password) {
  return availableFetch("/v1/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
    credentials: "same-origin",
  });
}

export async function logout() {
  const response = await availableFetch("/v1/logout", {
    method: "POST",
    headers: { [CSRF_HEADER]: "1" },
    credentials: "same-origin",
  }, { bypassCooldown: true });
  if (!response.ok && response.status !== 401) {
    throw new Error("Could not log out. Please try again.");
  }
}

export async function api(method, path, body, extraHeaders) {
  const request = async signal => {
    const headers = authenticatedHeaders(extraHeaders);
    if (body !== undefined) headers["Content-Type"] = "application/json";
    const response = await availableFetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: "same-origin",
      ...(signal ? { signal } : {}),
    });
    if (response.status === 401) { unauthorizedHandler(); throw new Error("unauthorized"); }
    let data;
    try {
      data = await response.json();
    } catch (_) {
      // Proxies can return HTML or an empty body. Preserve the HTTP status
      // instead of exposing Safari's unhelpful JSON parsing exception.
      const error = new Error(`Server returned an invalid JSON response (HTTP ${response.status}).`);
      error.status = response.status;
      throw error;
    }
    if (!response.ok) {
      const error = new Error(data.error ? data.error.message : response.statusText);
      error.status = response.status;
      throw error;
    }
    return data;
  };
  return method === "GET" ? withReadDeadline(request) : request();
}

export async function apiBlob(path) {
  const response = await availableFetch(path, {
    method: "GET",
    headers: authenticatedHeaders(),
    credentials: "same-origin",
  });
  if (response.status === 401) { unauthorizedHandler(); throw new Error("unauthorized"); }
  if (!response.ok) {
    let message = response.statusText;
    try {
      const data = await response.json();
      message = data.error ? data.error.message : message;
    } catch (_) {}
    const error = new Error(message);
    error.status = response.status;
    throw error;
  }
  return response.blob();
}

export async function apiUpload(file) {
  const response = await availableFetch(`/v1/agent-files/upload?filename=${encodeURIComponent(file.name)}`, {
    method: "POST",
    headers: authenticatedHeaders(),
    body: file,
    credentials: "same-origin",
  });
  let data = null;
  try {
    data = await response.json();
  } catch (_) {}
  if (response.status === 401) { unauthorizedHandler(); throw new Error("unauthorized"); }
  if (!response.ok) {
    throw new Error(data && data.error ? data.error.message : response.statusText || `upload failed (${response.status})`);
  }
  if (!data) throw new Error("file upload returned an invalid response");
  return data;
}
