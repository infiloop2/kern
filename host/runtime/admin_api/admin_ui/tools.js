import { api } from "./api.js";
import { $, badge, esc, inlineMessage, notice, replaceIntegrationRows, setHtml } from "./helpers.js";
import { applyIntegrationDetailSelection } from "./network.js";

let tools = [];
// Selection expands exactly one tool row; the remaining rows stay hidden.
const expandedTools = new Set();

export function selectToolDetail(guideId) {
  expandedTools.clear();
  if (guideId && guideId.startsWith("tool:")) expandedTools.add(guideId.slice(5));
}

function toolsMessage(toolId, message, isError) {
  const node = document.querySelector(`[data-tool-message="${toolId}"]`);
  inlineMessage(node, message, isError);
}

export async function refreshTools() {
  const response = await api("GET", "/v1/tools");
  tools = Array.isArray(response.tools) ? response.tools : [];
  renderTools();
}

function renderTools() {
  $("tools-cross-access-notice").hidden = tools.filter(tool => tool.enabled).length < 2;
  $("tools-empty").hidden = tools.length > 0;
  if (!tools.length) {
    replaceIntegrationRows($("tools"), "[data-tool-row]", "");
    return;
  }
  const sortedTools = [...tools]
    .sort((left, right) => left.display_name.localeCompare(right.display_name, undefined, { sensitivity: "base" }));
  replaceIntegrationRows($("tools"), "[data-tool-row]", sortedTools.map(renderToolRow).join(""));
  for (const tool of tools) {
    const status = document.querySelector(`[data-home-integration-status="tool:${tool.tool_id}"]`);
    if (!status) continue;
    status.className = `status ${tool.enabled ? "active" : "disabled"}`;
    status.textContent = tool.enabled ? "enabled" : "disabled";
  }
  document.dispatchEvent(new CustomEvent("kern-home-integration-statuses-updated"));
  applyIntegrationDetailSelection();
}

function renderToolRow(tool) {
  const expanded = expandedTools.has(tool.tool_id);
  const connections = Array.isArray(tool.connected_accounts) ? tool.connected_accounts : [];
  const connected = connections.length > 0;
  const chips = [badge(tool.enabled ? "enabled" : "disabled")];
  if (tool.connection === "oauth" && (tool.enabled || connected)) {
    chips.push(connected
      ? `<span class="status active">${connections.length} account${connections.length === 1 ? "" : "s"} connected</span>`
      : `<span class="status">not connected</span>`);
  }
  if (tool.connection === "whatsapp_linked_device") {
    const linked = tool.connection_status || {};
    chips.push(linked.connected
      ? `<span class="status active">connected</span>`
      : `<span class="status">${esc(linked.status || "not connected")}</span>`);
  }
  return `
    <section class="integration-row${expanded ? " expanded" : ""}" data-tool-row="${esc(tool.tool_id)}">
      <div class="integration-summary">
        <div class="integration-title">
          <h2>${esc(tool.display_name)}</h2>
          <div class="integration-subtitle">${esc(tool.description)}</div>
        </div>
        <span class="status-chips">${chips.join(" ")}</span>
        <span class="integration-actions">
          <span class="seg">
            <button data-action="enable-tool" data-tool="${esc(tool.tool_id)}"${tool.enabled ? " disabled" : ""}>Enable</button>
            <button data-action="disable-tool" data-tool="${esc(tool.tool_id)}"${tool.enabled ? "" : " disabled"}>Disable</button>
          </span>
        </span>
      </div>
      <p class="inline-message integration-row-message" data-tool-message="${esc(tool.tool_id)}" role="status" aria-live="polite"></p>
      <div class="integration-details" data-tool-details="${esc(tool.tool_id)}"${expanded ? "" : " hidden"}>
        <p class="muted">${esc(tool.description)}</p>
        ${tool.connection === "oauth" && (tool.enabled || connected) ? `
        <div class="detail-card">
          <div class="detail-card-head"><h3>Connection</h3></div>
          ${renderToolConnections(tool, connections)}
        </div>` : ""}
        ${tool.connection === "whatsapp_linked_device" ? `
        <div class="detail-card">
          <div class="detail-card-head"><h3>Linked device</h3></div>
          ${renderLinkedDeviceConnection(tool)}
        </div>` : ""}
        ${tool.config.length ? `
        <div class="detail-card">
          <div class="detail-card-head"><h3>Configuration</h3></div>
          <div class="tool-config">${tool.config.map(entry => renderToolConfigRow(tool, entry)).join("")}</div>
        </div>` : ""}

      </div>
    </section>`;
}

function renderLinkedDeviceConnection(tool) {
  const connection = tool.connection_status || {};
  const account = connection.account || {};
  const retainedData = connection.retained_data === true;
  const reconnecting = connection.status === "connecting" && Boolean(account.id || account.label);
  const qr = typeof connection.qr_data_url === "string" && connection.qr_data_url.startsWith("data:image/png;base64,")
    ? connection.qr_data_url : "";
  const summary = connection.connected
    ? `Connected as <span class="connection-identity">${esc(account.label || account.id || "WhatsApp account")}</span>. The gateway reconnects automatically after host restarts.`
    : connection.status === "suspended"
      ? `Linked as <span class="connection-identity">${esc(account.label || account.id || "WhatsApp account")}</span>. The session is retained, but the gateway is stopped while this integration is disabled.`
    : connection.status === "qr"
      ? "On your phone, open WhatsApp > Settings > Linked devices > Link a device, then scan this QR code."
    : reconnecting
      ? `Reconnecting <span class="connection-identity">${esc(account.label || account.id || "WhatsApp account")}</span>. No QR code is needed.`
    : connection.status === "connecting"
      ? "Starting WhatsApp. The QR code will appear here when it is ready."
      : esc(connection.error || "No WhatsApp account is linked.");
  return `
    <p class="connection-summary">${summary}</p>
    ${qr ? `<div class="linked-device-qr"><img src="${esc(qr)}" alt="WhatsApp linked-device QR code"></div>` : ""}
    ${connection.status === "connecting" && !reconnecting ? `<div class="linked-device-qr linked-device-qr-loading" role="status">Waiting for QR code…</div>` : ""}
    <div class="integration-account-actions">
      ${connection.connected || connection.status === "suspended"
        ? `<button class="ghost sm" data-action="refresh-linked-device" data-tool="${esc(tool.tool_id)}">Refresh status</button>
           <button class="danger ghost sm" data-action="disconnect-linked-device" data-tool="${esc(tool.tool_id)}">Disconnect</button>`
        : reconnecting
          ? `<button class="ghost sm" data-action="refresh-linked-device" data-tool="${esc(tool.tool_id)}">Refresh status</button>
             <button class="danger ghost sm" data-action="disconnect-linked-device" data-tool="${esc(tool.tool_id)}">Disconnect</button>`
        : connection.status === "connecting"
          ? `<button class="primary sm" disabled>Waiting for QR code…</button>
             ${retainedData ? `<button class="danger ghost sm" data-action="disconnect-linked-device" data-tool="${esc(tool.tool_id)}">Disconnect</button>` : ""}`
          : `<button class="primary sm" data-action="${connection.status === "qr" ? "refresh-linked-device" : "connect-linked-device"}" data-tool="${esc(tool.tool_id)}"${tool.enabled ? "" : " disabled"}>${connection.status === "qr" ? "Check status / refresh QR" : "Link device"}</button>
             ${connection.status === "qr" || retainedData ? `<button class="danger ghost sm" data-action="disconnect-linked-device" data-tool="${esc(tool.tool_id)}">Disconnect</button>` : ""}`}
    </div>`;
}

// The OAuth connection line mirrors the provider linked-account line in the
// managed integration dropdowns. Disconnect stays available whenever an
// account is connected, even if the tool was later disabled or its config
// cleared, so the operator always has a path to revoke and delete stored
// tokens (the backend allows it too). Connect requires the tool to be enabled.
function renderToolConnections(tool, connections) {
  const rows = connections.map(connection => {
    const account = connection.account || {};
    return `<div class="integration-account">
      <p class="connection-summary">
        <span class="connection-identity">${esc(account.label || account.id || "Connected account")}</span>
        <span class="muted mono">${esc(connection.connection_id || "")}</span>
      </p>
      <span class="integration-account-actions">
        <button class="ghost sm" data-action="connect-tool" data-tool="${esc(tool.tool_id)}" data-connection="${esc(connection.connection_id || "")}"${tool.enabled ? "" : " disabled"}>Reconnect</button>
        <button class="danger ghost sm" data-action="disconnect-tool" data-tool="${esc(tool.tool_id)}" data-connection="${esc(connection.connection_id || "")}">Disconnect</button>
      </span>
    </div>`;
  }).join("");
  const empty = connections.length ? "" : `
    <p class="connection-summary">No account connected yet. Connect signs in on the provider's site and stores the tokens on the host.</p>`;
  return `${rows}${empty}<div class="integration-account">
    <p class="connection-summary muted">Each account gets a stable connection id that agents use to target reads and approved writes.</p>
    <button class="primary sm" data-action="connect-tool" data-tool="${esc(tool.tool_id)}"${tool.enabled ? "" : " disabled"}>${connections.length ? "Connect another account" : "Connect account"}</button>
  </div>`;
}

function renderToolConfigRow(tool, entry) {
  const inputId = `tool-config-${tool.tool_id}-${entry.key}`;
  return `
    <div class="tool-config-row">
      <label class="field" for="${esc(inputId)}">
        <span class="config-key mono">${esc(entry.key)} ${entry.set ? `<span class="status active">set</span>` : `<span class="status">not set</span>`}</span>
        <span class="muted config-note">${esc(entry.description)}</span>
      </label>
      <div class="config-input-row">
        <input id="${esc(inputId)}" type="password"
               placeholder="${entry.set ? "configured (enter to replace, blank to clear)" : "not configured"}" spellcheck="false">
        <button class="sm" data-action="save-tool-config" data-tool="${esc(tool.tool_id)}" data-key="${esc(entry.key)}">Save</button>
      </div>
    </div>`;
}

export async function setToolEnabled(toolId, enabled) {
  const label = tools.find(tool => tool.tool_id === toolId)?.display_name || toolId;
  try {
    toolsMessage(toolId, "");
    await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/${enabled ? "enable" : "disable"}`, {});
    await refreshTools();
    toolsMessage(toolId, `${label} ${enabled ? "enabled" : "disabled"}.`);
  } catch (error) {
    try { await refreshTools(); } catch (_refreshError) { /* keep the original action error */ }
    toolsMessage(toolId, error.message, true);
  }
}

export async function saveToolConfig(toolId, key) {
  const input = $(`tool-config-${toolId}-${key}`);
  const value = input.value.trim();
  try {
    toolsMessage(toolId, "");
    await api("PUT", `/v1/tools/${encodeURIComponent(toolId)}/config`, { key, value });
    input.value = "";
    await refreshTools();
    toolsMessage(toolId, `${key} ${value ? "saved" : "cleared"}.`);
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

function oauthRedirectUri() {
  return location.origin + "/oauth/callback";
}

export async function connectTool(toolId, connectionId = "") {
  try {
    toolsMessage(toolId, "");
    const body = { redirect_uri: oauthRedirectUri() };
    if (connectionId) body.connection_id = connectionId;
    const response = await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/oauth_connect/start`, body);
    if (!response.connection_id) throw new Error("Connect did not return a connection id.");
    sessionStorage.setItem("kern_tool_connect", toolId);
    sessionStorage.setItem("kern_tool_connection", response.connection_id);
    location.assign(response.authorization_url);
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

export async function disconnectTool(toolId, connectionId) {
  if (!confirm("Disconnect this account? Stored tokens are revoked and deleted.")) return;
  try {
    toolsMessage(toolId, "");
    await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/oauth_connect/disconnect`, {
      connection_id: connectionId,
    });
    await refreshTools();
    toolsMessage(toolId, "Account disconnected.");
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

export async function connectLinkedDevice(toolId) {
  const tool = tools.find(entry => entry.tool_id === toolId);
  const previousConnection = tool?.connection_status;
  try {
    if (tool) {
      tool.connection_status = {
        ...(tool.connection_status || {}),
        status: "connecting",
        connected: false,
        error: "",
      };
      renderToolsPreservingConfigInputs();
    }
    toolsMessage(toolId, "The QR code will appear below in Linked device.");
    const response = await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/service/connect`, {});
    if (tool) {
      tool.connection_status = response;
      renderToolsPreservingConfigInputs();
    } else {
      await refreshTools();
    }
    if (response.status === "qr") {
      toolsMessage(toolId, "Scan the QR code shown below in Linked device with the WhatsApp account you want Kern to use.");
    } else if (response.status === "connected") {
      toolsMessage(toolId, "WhatsApp connected.");
    } else if (response.status === "connecting") {
      toolsMessage(toolId, "WhatsApp is still connecting. This page will show the QR code when it is ready.");
    } else {
      toolsMessage(toolId, response.error || "WhatsApp is not connected. Retry linking.", true);
    }
  } catch (error) {
    if (tool) {
      tool.connection_status = previousConnection || {};
      renderToolsPreservingConfigInputs();
    }
    toolsMessage(toolId, error.message, true);
  }
}

export async function refreshLinkedDevice(toolId) {
  try {
    toolsMessage(toolId, "");
    await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/service/status`, {});
    await refreshTools();
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

export async function disconnectLinkedDevice(toolId) {
  if (!confirm("Disconnect WhatsApp? Kern will log out the linked device and delete its local session keys and cached messages.")) return;
  try {
    toolsMessage(toolId, "");
    await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/service/disconnect`, {});
    await refreshTools();
    toolsMessage(toolId, "WhatsApp disconnected and local linked-device data deleted.");
  } catch (error) { toolsMessage(toolId, error.message, true); }
}

// Finish a tool OAuth connect after the provider redirected back to
// /oauth/callback?code=...&state=... — the tool id was stashed before leaving.
// app.js preserves the callback query before replacing the callback URL and
// waits for the focused tool row to render before completing the exchange.
export async function completeToolConnect(callbackSearch = location.search) {
  const params = new URLSearchParams(callbackSearch);
  const toolId = sessionStorage.getItem("kern_tool_connect");
  const connectionId = sessionStorage.getItem("kern_tool_connection");
  sessionStorage.removeItem("kern_tool_connect");
  sessionStorage.removeItem("kern_tool_connection");
  if (!toolId || !connectionId) { notice("Tool connect callback had no pending connection."); return; }
  if (!params.get("code")) {
    try { await refreshTools(); } catch (_error) { /* render the callback error if the row already exists */ }
    toolsMessage(toolId, `Connect cancelled: ${params.get("error") || "no authorization code returned"}.`, true);
    return;
  }
  let message = "";
  let isError = false;
  try {
    const result = await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/oauth_connect/complete`, {
      code: params.get("code"),
      state: params.get("state") || "",
      redirect_uri: oauthRedirectUri(),
      connection_id: connectionId,
    });
    const label = result.account && result.account.label;
    message = `Connected ${toolId}${label ? ` as ${label}` : ""}.`;
  } catch (error) {
    message = error.message;
    isError = true;
  }
  try { await refreshTools(); } catch (_error) { /* show the callback result if the row already exists */ }
  toolsMessage(toolId, message, isError);
}

function renderToolsPreservingConfigInputs() {
  const values = [...document.querySelectorAll('input[id^="tool-config-"]')]
    .map(input => [input.id, input.value]);
  renderTools();
  for (const [id, value] of values) {
    const input = document.getElementById(id);
    if (input) input.value = value;
  }
}

export async function refreshExpandedLinkedDevices() {
  for (const toolId of expandedTools) {
    const tool = tools.find(entry => entry.tool_id === toolId);
    const current = tool?.connection_status || {};
    const cleanupPending = current.status === "disconnected" && current.retained_data === true;
    if (
      tool?.connection !== "whatsapp_linked_device"
      || (!current.connected && !["connecting", "qr"].includes(current.status) && !cleanupPending)
    ) continue;
    try {
      const status = await api("POST", `/v1/tools/${encodeURIComponent(toolId)}/service/status`, {});
      if (JSON.stringify(status) === JSON.stringify(current)) continue;
      tool.connection_status = status;
      renderToolsPreservingConfigInputs();
      if (status.connected && !current.connected) {
        toolsMessage(toolId, "WhatsApp linked successfully.");
      }
    } catch (_error) { /* the visible action retains explicit retry/error feedback */ }
  }
}
