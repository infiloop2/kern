// Browser-only WebAuthn conversion and the two passkey ceremonies. Password
// handling remains in app.js; this module never sees or stores it.

import { api } from "./api.js";
import { $, notice } from "./helpers.js";

const CSRF_HEADER = "X-Kern-Csrf";

function decode(value) {
  const padded = value.replaceAll("-", "+").replaceAll("_", "/")
    + "=".repeat((4 - value.length % 4) % 4);
  const binary = atob(padded);
  return Uint8Array.from(binary, character => character.charCodeAt(0));
}

function encode(value) {
  if (value === null || value === undefined) return null;
  const bytes = new Uint8Array(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replaceAll("=", "");
}

function browserOptions(options) {
  return {
    ...options,
    challenge: decode(options.challenge),
    user: options.user ? { ...options.user, id: decode(options.user.id) } : undefined,
    allowCredentials: (options.allowCredentials || []).map(
      credential => ({ ...credential, id: decode(credential.id) }),
    ),
    excludeCredentials: (options.excludeCredentials || []).map(
      credential => ({ ...credential, id: decode(credential.id) }),
    ),
  };
}

function serializedCredential(credential) {
  const response = credential.response;
  const serialized = {
    id: encode(credential.rawId),
    rawId: encode(credential.rawId),
    type: credential.type,
    authenticatorAttachment: credential.authenticatorAttachment || null,
    response: {
      clientDataJSON: encode(response.clientDataJSON),
    },
  };
  if (response.attestationObject) {
    serialized.response.attestationObject = encode(response.attestationObject);
    serialized.response.transports = response.getTransports ? response.getTransports() : [];
  } else {
    serialized.response.authenticatorData = encode(response.authenticatorData);
    serialized.response.signature = encode(response.signature);
    serialized.response.userHandle = encode(response.userHandle);
  }
  return serialized;
}

function requireWebAuthn() {
  if (!window.PublicKeyCredential || !navigator.credentials) {
    throw new Error("This browser does not support passkeys.");
  }
}

export async function finishPasskeyLogin(options) {
  requireWebAuthn();
  const credential = await navigator.credentials.get({
    publicKey: browserOptions(options),
  });
  if (!credential) throw new Error("No passkey was selected.");
  return fetch("/v1/login/passkey", {
    method: "POST",
    headers: {
      "Content-Type": "application/json",
      [CSRF_HEADER]: "1",
    },
    body: JSON.stringify(serializedCredential(credential)),
    credentials: "same-origin",
  });
}

export async function refreshLoginPasskeyStatus() {
  const badge = $("login-passkey-status");
  badge.hidden = true;
  try {
    const response = await fetch("/v1/login/status", {
      credentials: "same-origin",
      headers: { "Accept": "application/json" },
    });
    if (!response.ok) return;
    const status = await response.json();
    badge.hidden = status.passkey_configured !== true;
  } catch (_) {
    // The SSH-forward origin intentionally has no public-login status, and a
    // transient failure must never prevent the operator from logging in.
  }
}

export async function refreshPasskeySetup() {
  const banner = $("passkey-setup");
  const control = $("passkey-status-control");
  try {
    const status = await api("GET", "/v1/admin-passkeys");
    const available = status.setup_available === true;
    const configured = status.configured === true;
    banner.hidden = configured || !available;
    control.hidden = !available;
    control.dataset.configured = String(configured);
    control.classList.toggle("passkey-protected", configured);
    control.classList.toggle("passkey-setup-needed", !configured);
    const title = configured
      ? "Public login is protected by 2FA"
      : "Make public login more secure";
    const detail = configured
      ? "Your admin password and a passkey are required."
      : "Set up a passkey as your second factor.";
    $("passkey-status-title").textContent = title;
    $("passkey-status-detail").textContent = detail;
    control.setAttribute("aria-label", `${title}. ${detail}`);
  } catch (_) {
    banner.hidden = true;
    control.hidden = true;
  }
}

export function showPasskeyGuidance() {
  const control = $("passkey-status-control");
  if (control.dataset.configured === "true") {
    notice("Public login is protected by your admin password and passkey.", "ok");
    return;
  }
  const banner = $("passkey-setup");
  if (banner.hidden) return;
  banner.scrollIntoView({ behavior: "smooth", block: "start" });
  $("setup-passkey").focus({ preventScroll: true });
}

export async function setupPasskey() {
  const button = $("setup-passkey");
  button.disabled = true;
  try {
    requireWebAuthn();
    const options = await api("POST", "/v1/admin-passkeys/register/options");
    const credential = await navigator.credentials.create({
      publicKey: browserOptions(options.publicKey),
    });
    if (!credential) throw new Error("Passkey setup was cancelled.");
    await api(
      "POST",
      "/v1/admin-passkeys/register",
      serializedCredential(credential),
    );
    await refreshPasskeySetup();
    notice("Passkey protection is on for public admin logins.", "ok");
  } catch (error) {
    if (error && error.name === "NotAllowedError") {
      notice("Passkey setup was cancelled.", "error");
    } else {
      notice(error.message || "Passkey setup failed.", "error");
    }
  } finally {
    button.disabled = false;
  }
}
