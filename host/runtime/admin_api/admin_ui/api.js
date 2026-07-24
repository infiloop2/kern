// Backend access: the one fetch wrapper every module calls. Authentication is
// an HttpOnly session cookie the browser sends automatically after /v1/login;
// this code never reads or holds the admin password. Every request carries the
// CSRF header so the server accepts the cookie (a cross-site page cannot set
// it). app.js registers what happens on a 401 (show the login screen).

const CSRF_HEADER = "X-TrustyClaw-Csrf";

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
  const headers = { [CSRF_HEADER]: "1" };
  for (const [name, value] of Object.entries(extraHeaders || {})) headers[name] = value;
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
    headers: { [CSRF_HEADER]: "1" },
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
    headers: { [CSRF_HEADER]: "1" },
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
