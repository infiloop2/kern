// Read-only database storage snapshot. Loaded on entry or explicit refresh;
// table contents and cleanup controls are deliberately absent.
import { api } from "./api.js";
import { $, esc, setHtml } from "./helpers.js";

function size(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  const unit = Math.min(Math.floor(Math.log(bytes) / Math.log(1024)), 4);
  return `${(bytes / 1024 ** unit).toLocaleString(undefined, { maximumFractionDigits: 1 })} ${["B", "KiB", "MiB", "GiB", "TiB"][unit]}`;
}

export async function refreshDatabaseStorage() {
  const button = $("database-storage-refresh");
  if (button.disabled) return;
  button.disabled = true;
  const status = $("database-storage-status");
  status.textContent = "Loading database storage…";
  try {
    const response = await api("GET", "/v1/database-storage");
    $("database-storage-total").textContent = `${size(response.database_bytes)} database storage`;
    setHtml($("database-storage-tables"), `<thead><tr><th scope="col">Table / contents</th><th scope="col">Estimated rows</th><th scope="col">Size</th><th scope="col">Retention / what can be deleted</th></tr></thead><tbody>` +
      response.tables.map(table => {
        const target = table.row_target && table.estimated_rows !== null
          ? `<div class="muted">≈${esc((table.estimated_rows / table.row_target * 100).toLocaleString(undefined, { maximumFractionDigits: 1 }))}% of history target</div>` : "";
        return `<tr>
          <td><div class="mono">${esc(table.name)}</div><div class="muted">${esc(table.description)}</div></td>
          <td>${table.estimated_rows === null ? "Unavailable" : esc(table.estimated_rows.toLocaleString())}</td>
          <td>${esc(size(table.total_bytes))}</td>
          <td>${esc(table.retention)}${target}</td>
        </tr>`;
      }).join("") + `</tbody>`);
    status.textContent = `Updated ${new Date().toLocaleTimeString()}. ${response.tables.length} tables, largest first.`;
  } catch (error) {
    status.textContent = "Could not refresh database storage. Any displayed figures are from the previous successful load. Try Refresh.";
    throw error;
  } finally {
    button.disabled = false;
  }
}
