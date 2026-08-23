import { $ } from "./helpers.js";

const IOS_INSTALL_DISMISSED_AT = "kern.ios-install-dismissed-at.v1";
const IOS_INSTALL_REMIND_AFTER_MS = 30 * 24 * 60 * 60 * 1000;
let iosInstallTimer = null;
let iosInstallReturnFocus = null;

export function isIPhoneStandalone() {
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

export function hideIPhoneInstallUi() {
  $("ios-install-coach").hidden = true;
  $("ios-install-overlay").hidden = true;
  document.body.classList.remove("install-guide-open");
}

export function scheduleIPhoneInstallCoach() {
  hideIPhoneInstallUi();
  if (iosInstallTimer) clearTimeout(iosInstallTimer);
  if (!shouldOfferIPhoneInstall()) return;
  iosInstallTimer = setTimeout(() => {
    iosInstallTimer = null;
    if (!$("app").hidden && shouldOfferIPhoneInstall()) $("ios-install-coach").hidden = false;
  }, 3500);
}

export function showIPhoneInstallGuide(trigger) {
  iosInstallReturnFocus = trigger || null;
  $("ios-install-coach").hidden = true;
  $("ios-install-overlay").hidden = false;
  document.body.classList.add("install-guide-open");
  $("ios-install-done").focus();
}

export function closeIPhoneInstallGuide() {
  $("ios-install-overlay").hidden = true;
  document.body.classList.remove("install-guide-open");
  if (iosInstallReturnFocus?.isConnected) iosInstallReturnFocus.focus();
  iosInstallReturnFocus = null;
  if (shouldOfferIPhoneInstall()) $("ios-install-coach").hidden = false;
}

export function dismissIPhoneInstall() {
  try { localStorage.setItem(IOS_INSTALL_DISMISSED_AT, String(Date.now())); }
  catch (_error) { /* Dismissal remains valid for this page load. */ }
  hideIPhoneInstallUi();
  iosInstallReturnFocus = null;
}
