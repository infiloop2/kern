import { api } from "./api.js";
import { esc } from "./helpers.js";
import { browserConnectionPanel, refreshBrowserConnection } from "./browser_connection.js";
let popup = null;
export function browserPanel(enabled) {
  return `${browserConnectionPanel()}<div class="detail-card"><div class="detail-card-head"><h3>X connections</h3><button data-browser="refresh">Refresh</button></div>
    <p class="muted">Sign in to X in a private browser popup. Save up to 5 separate accounts on this host.</p>
    <div id="browser-sessions">Choose Refresh to load saved accounts.</div>
    <form id="browser-connect-form" class="browser-connect"><button type="submit" class="primary" data-enabled="${enabled}" ${enabled ? "" : "disabled"}>Connect X account</button></form>
    <p id="browser-settings-message" role="status"></p></div>`;
}
function message(value) { const el = document.getElementById("browser-settings-message"); if (el) el.textContent = value; }
export async function refreshBrowserSessions() {
  const node = document.getElementById("browser-sessions");
  if (!node) return;
  await refreshBrowserConnection();
  try {
    const {accounts} = await api("POST", "/v1/browser/list", {});
    const connect = document.querySelector('#browser-connect-form button');
    if (connect) connect.disabled = connect.dataset.enabled !== "true" || accounts.length >= 5;
    node.innerHTML = accounts.map(account => `<section class="browser-session" data-account="${esc(account.account_id)}">
      <div><strong>${esc(account.provider_identifier ? `@${account.provider_identifier}` : "X account")}</strong><p>${esc(account.state === "connected" ? "Login verified" : "Login needs attention; agent actions paused")}${account.checked_at ? ` · Last checked ${esc(new Date(account.checked_at).toLocaleString())}` : ""}</p></div>
      <div class="browser-buttons"><button data-browser="open">Open browser</button>${account.provider_identifier ? '<button data-browser="check">Check login</button>' : ""}<button data-browser="disconnect" class="danger ghost">Disconnect</button></div>
    </section>`).join("") || '<p class="muted">No saved X accounts yet.</p>';
  } catch (error) { message(error.message); }
}
function openPopup(accountId) {
  if (popup && !popup.closed) { popup.focus(); message("Finish or close the open browser first."); return; }
  popup = window.open(`/browser.html?account=${encodeURIComponent(accountId)}`, "kern-browser", "popup,width=1160,height=950");
  if (!popup) message("Your browser blocked the popup. Allow popups for Kern, then choose Open browser.");
}
document.addEventListener("submit", async event => {
  if (event.target.id !== "browser-connect-form") return;
  event.preventDefault();
  if (popup && !popup.closed) { popup.focus(); message("Finish or close the open browser first."); return; }
  // Reserve the popup in the trusted click before awaiting network work.
  popup = window.open("about:blank", "kern-browser", "popup,width=1160,height=950");
  if (!popup) { message("Allow popups for Kern, then choose Connect X account again."); return; }
  try {
    const login = await api("POST", "/v1/browser/create", {provider: "x"});
    popup.location.href = `/browser.html?login=${encodeURIComponent(login.login_id)}`;
    await refreshBrowserSessions();
  } catch (error) { popup?.close(); message(error.message); }
});
document.addEventListener("click", async event => {
  const button = event.target.closest("[data-browser]");
  if (!button) return;
  const row = button.closest("[data-account]"), account_id = row?.dataset.account;
  const operation = button.dataset.browser;
  if (operation === "open") { openPopup(account_id); return; }
  button.disabled = true;
  try {
    if (operation === "refresh") { await refreshBrowserSessions(); return; }
    if (operation === "disconnect") {
      if (!window.confirm("Disconnect this X account and delete its saved login and usage data?")) return;
      await api("POST", "/v1/browser/disconnect", {account_id});
    } else if (operation === "check") {
      const state = await api("POST", "/v1/browser/check", {account_id});
      message(state.state === "connected" ? `Login verified for @${state.provider_identifier}.` : "Login needs attention. Open the browser to sign in again.");
    }
    await refreshBrowserSessions();
  } catch (error) { message(error.message); }
  finally { button.disabled = false; }
});
window.addEventListener("message", event => { if (event.origin === location.origin && event.source === popup && event.data?.type === "kern-browser-saved") refreshBrowserSessions(); });
