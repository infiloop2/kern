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

// POST the password to mint a session cookie. Returns the raw Response so the
// caller can distinguish a wrong password (401) from a throttled attempt (429).
export function login(password) {
  return fetch("/v1/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ password }),
    credentials: "same-origin",
  });
}

export async function logout() {
  await fetch("/v1/logout", {
    method: "POST",
    headers: { [CSRF_HEADER]: "1" },
    credentials: "same-origin",
  });
}

export async function api(method, path, body, extraHeaders) {
  const headers = authenticatedHeaders(extraHeaders);
  if (body !== undefined) headers["Content-Type"] = "application/json";
  const response = await fetch(path, {
    method,
    headers,
    body: body === undefined ? undefined : JSON.stringify(body),
    credentials: "same-origin",
  });
  const data = await response.json();
  if (response.status === 401) { unauthorizedHandler(); throw new Error("unauthorized"); }
  if (!response.ok) throw new Error(data.error ? data.error.message : response.statusText);
  return data;
}

export async function apiBlob(path) {
  const response = await fetch(path, {
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
    throw new Error(message);
  }
  return response.blob();
}

export async function apiUpload(file) {
  const response = await fetch(`/v1/agent-files/upload?filename=${encodeURIComponent(file.name)}`, {
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
