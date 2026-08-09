// Agent workspace tab: read-only file explorer over the agent home.

import { api, apiBlob } from "./api.js";
import { $ } from "./helpers.js";

const FILE_LIST_ENTRY_LIMIT = 1000;

let currentFilePath = "/";
let fileEntries = [];
let activeFileUrl = null;
let fileNavigationSequence = 0;

function fileMessage(message, isError) {
  const node = $("file-message");
  node.textContent = message || "";
  node.classList.toggle("error", isError === true);
}

function parentPath(path) {
  const normalized = path && path !== "/" ? path.replace(/\/+$/, "") : "/";
  if (normalized === "/") return "/";
  const index = normalized.lastIndexOf("/");
  return index <= 0 ? "/" : normalized.slice(0, index);
}

export async function loadAgentFiles(path = currentFilePath, navigate = false) {
  if (navigate) fileNavigationSequence += 1;
  const navigationSequence = fileNavigationSequence;
  try {
    fileMessage("");
    const response = await api("GET", `/v1/agent-files?path=${encodeURIComponent(path || "/")}`);
    if (
      navigationSequence !== fileNavigationSequence
      || (!navigate && path !== currentFilePath)
    ) return;
    currentFilePath = response.path || "/";
    fileEntries = Array.isArray(response.entries) ? response.entries : [];
    $("file-path").value = currentFilePath;
    renderFileList(response);
  } catch (error) {
    if (
      navigationSequence !== fileNavigationSequence
      || (!navigate && path !== currentFilePath)
    ) return;
    fileMessage(error.message, true);
  }
}

export function refreshFiles() {
  return loadAgentFiles(currentFilePath);
}

export async function ensureFilesLoaded() {
  if (!fileEntries.length) await loadAgentFiles(currentFilePath);
}

export function loadParentDirectory() {
  return loadAgentFiles(parentPath(currentFilePath), true);
}

async function readAgentFile(path) {
  try {
    fileMessage("");
    const isVideo = /\.(mp4|mov)$/i.test(path);
    const isImage = /\.(jpe?g|png|webp)$/i.test(path);
    if (isVideo || isImage) {
      const blob = await apiBlob(`/v1/agent-files/content?path=${encodeURIComponent(path)}`);
      if (isVideo) {
        if (!["video/mp4", "video/quicktime"].includes(blob.type)) {
          throw new Error("file is not a supported video");
        }
        renderFileVideo(path, blob);
      } else {
        if (!["image/jpeg", "image/png", "image/webp"].includes(blob.type)) {
          throw new Error("file is not a supported image");
        }
        renderFileImage(path, blob);
      }
      return;
    }
    const response = await api("GET", `/v1/agent-files/read?path=${encodeURIComponent(path)}`);
    renderFileContent(response);
  } catch (error) {
    fileMessage(error.message, true);
  }
}

export async function openAgentPath(path, type) {
  if (type === "directory") {
    await loadAgentFiles(path, true);
    return;
  }
  await readAgentFile(path);
}

function renderFileList(listing = {}) {
  if (listing.truncated) {
    fileMessage(`Showing first ${FILE_LIST_ENTRY_LIMIT} entries.`);
  }
  const table = $("file-list");
  table.textContent = "";
  const header = document.createElement("tr");
  for (const label of ["name", "type", "size"]) {
    const cell = document.createElement("th");
    cell.textContent = label;
    header.appendChild(cell);
  }
  table.appendChild(header);
  if (currentFilePath !== "/") {
    table.appendChild(fileRow("..", parentPath(currentFilePath), "directory", null));
  }
  if (!fileEntries.length) {
    const row = document.createElement("tr");
    const cell = document.createElement("td");
    cell.colSpan = 3;
    cell.className = "empty-state";
    cell.textContent = "Empty directory.";
    row.appendChild(cell);
    table.appendChild(row);
    return;
  }
  for (const entry of fileEntries) {
    table.appendChild(fileRow(entry.name, entry.path, entry.type, entry.size_bytes));
  }
}

function fileRow(name, path, type, sizeBytes) {
  const row = document.createElement("tr");
  const nameCell = document.createElement("td");
  const button = document.createElement("button");
  button.className = "file-entry";
  button.dataset.action = "open-file-path";
  button.dataset.path = path == null ? "" : String(path);
  button.dataset.fileType = type == null ? "" : String(type);
  button.textContent = name == null ? "" : String(name);
  nameCell.appendChild(button);
  row.appendChild(nameCell);

  const typeCell = document.createElement("td");
  typeCell.textContent = type == null ? "" : String(type);
  row.appendChild(typeCell);

  const sizeCell = document.createElement("td");
  sizeCell.className = "muted";
  sizeCell.textContent = sizeBytes == null ? "" : String(sizeBytes);
  row.appendChild(sizeCell);
  return row;
}

function renderFileContent(file) {
  resetFileMedia();
  $("file-content").hidden = false;
  const truncated = file.truncated ? " (truncated)" : "";
  $("file-viewer-title").textContent = `${file.path || ""}${truncated}`;
  $("file-content").textContent = file.content || "";
}

function renderFileVideo(path, blob) {
  resetFileMedia();
  activeFileUrl = URL.createObjectURL(blob);
  $("file-viewer-title").textContent = path;
  $("file-content").hidden = true;
  const video = $("file-video");
  video.src = activeFileUrl;
  video.hidden = false;
}

function renderFileImage(path, blob) {
  resetFileMedia();
  activeFileUrl = URL.createObjectURL(blob);
  $("file-viewer-title").textContent = path;
  $("file-content").hidden = true;
  const image = $("file-image");
  image.src = activeFileUrl;
  image.alt = path;
  image.hidden = false;
}

function resetFileMedia() {
  if (activeFileUrl) URL.revokeObjectURL(activeFileUrl);
  activeFileUrl = null;
  const video = $("file-video");
  video.hidden = true;
  video.removeAttribute("src");
  const image = $("file-image");
  image.hidden = true;
  image.removeAttribute("src");
  image.alt = "";
}

export function goToFilePath() {
  loadAgentFiles($("file-path").value.trim() || "/", true);
}
