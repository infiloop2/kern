import { api } from "./api.js";

export function browserConnectionPanel() {
  return `<div class="detail-card"><h3>Browser connection</h3>
    <p class="muted">A residential proxy is preferred when Kern runs on AWS or another datacenter host, where websites may restrict the server's IP. Decodo is optional; websites can still require verification.</p>
    <form id="browser-connection-form">
      <label>Connection<select name="mode" required disabled><option value="" disabled>Select a connection</option><option value="direct">Direct (this host)</option><option value="decodo">Decodo Residential</option></select></label>
      <fieldset data-connection="decodo" hidden disabled>
        <p>Use credentials from Decodo's Residential product and choose your location.</p>
        <label>Proxy username<input name="username" placeholder="example" maxlength="128" required autocomplete="off" spellcheck="false"></label>
        <label>Proxy password<input name="password" type="password" maxlength="1024" autocomplete="new-password"></label>
        <label>Location<select name="location" required><option value="">Choose a location</option></select></label>
        <p class="muted">New York and London each set a matching country, city, browser language and timezone automatically.</p>
        <p class="muted">Enter the base proxy username, without country, city or session parameters. Leave the password blank to keep it for the same username.</p>
        <p class="muted">Kern uses the Residential gateway with encrypted proxy authentication and a sticky session of up to 24 hours. IPs can change; provider availability and website geolocation may differ. Static ISP and datacenter endpoints are not supported.</p>
      </fieldset>
      <p class="muted">This choice applies to all Browser accounts. Save and close any browser popup first. A failed connection never falls back to Direct.</p>
      <div class="browser-buttons"><button type="submit" class="primary" disabled>Save connection</button><button type="button" data-connection-test disabled>Test saved connection</button></div>
    </form><p id="browser-connection-message" role="status">Loading connection settings...</p></div>`;
}
function message(text) { const node = document.getElementById("browser-connection-message"); if (node) node.textContent = text; }
function selectMode(form) {
  const mode = form.elements.mode.value;
  for (const group of form.querySelectorAll("[data-connection]")) {
    group.hidden = group.dataset.connection !== mode;
    group.disabled = group.hidden;
  }
}
function populate(form, value) {
  form.elements.mode.value = value.mode;
  form.elements.username.value = value.username || "";
  form.elements.location.replaceChildren(new Option("Choose a location", ""),
    ...value.locations.map(location => new Option(location.label, location.id)));
  form.elements.location.value = value.location || "";
  form.elements.password.value = "";
  form.elements.password.placeholder = value.has_password ? "Saved; leave blank to keep" : "Proxy password";
  selectMode(form);
}
export async function refreshBrowserConnection() {
  const form = document.getElementById("browser-connection-form");
  if (!form || form.dataset.loaded) return;
  form.dataset.loaded = "loading";
  try {
    const value = await api("POST", "/v1/browser/network_get", {});
    if (!form.isConnected) return;
    populate(form, value);
    form.elements.mode.disabled = false;
    for (const button of form.querySelectorAll("button")) button.disabled = false;
    form.dataset.loaded = "true";
    message(value.error || "Connection applies to future Browser activity.");
  } catch (error) { delete form.dataset.loaded; message(error.message); }
}
document.addEventListener("change", event => {
  const form = event.target.closest("#browser-connection-form");
  if (form && event.target.name === "mode") selectMode(form);
});
async function run(form, operation) {
  for (const button of form.querySelectorAll("button")) button.disabled = true;
  message(operation === "test" ? "Testing saved connection..." : "Saving connection...");
  try {
    const body = operation === "save" ? Object.fromEntries(new FormData(form)) : {};
    const value = await api("POST", `/v1/browser/network_${operation}`, body);
    if (!form.isConnected) return;
    if (operation === "save") { populate(form, value); message("Connection saved. Test it before opening Browser."); }
    else message(`Connection works. Outgoing IP: ${value.ip}. This checks connectivity, not website login or city accuracy.`);
  } catch (error) { message(error.message); }
  finally {
    form.elements.password.value = "";
    for (const button of form.querySelectorAll("button")) button.disabled = false;
  }
}
document.addEventListener("submit", event => {
  if (event.target.id !== "browser-connection-form") return;
  event.preventDefault();
  run(event.target, "save");
});
document.addEventListener("click", event => {
  if (event.target.closest("[data-connection-test]")) run(event.target.closest("form"), "test");
});
