// Chat-only workspace previews. Fetch through the same authenticated API as
// Files; never put agent-provided URLs directly into an image's src.
import { apiBlob } from "./api.js";

const states = new Map();
let activeRequests = 0;
let viewer = null;
let viewerUrl = "";
let viewerOpener = null;

function closeViewer() {
  if (viewer?.open) viewer.close();
}

function expandImage(state) {
  if (!state.blob) return;
  if (!viewer) {
    viewer = document.createElement("dialog");
    viewer.className = "chat-image-viewer";
    viewer.setAttribute("aria-label", "Image viewer");
    viewer.innerHTML = `<header><span class="chat-image-viewer-title"></span>
      <button type="button" autofocus aria-label="Close image viewer">Close</button></header>
      <div class="chat-image-viewer-body"><img alt=""></div>`;
    document.body.append(viewer);
    viewer.querySelector("button").addEventListener("click", closeViewer);
    viewer.querySelector(".chat-image-viewer-body").addEventListener("click", event => {
      if (event.target === event.currentTarget) closeViewer();
    });
    viewer.addEventListener("keydown", event => {
      if (event.key === "Tab") {
        event.preventDefault();
        viewer.querySelector("button").focus();
      }
    });
    viewer.addEventListener("close", () => {
      viewer.querySelector("img").removeAttribute("src");
      URL.revokeObjectURL(viewerUrl);
      viewerUrl = "";
      document.body.classList.remove("chat-image-viewer-open");
      if (viewerOpener?.isConnected) viewerOpener.focus({ preventScroll: true });
      viewerOpener = null;
    });
  }
  viewerOpener = state.button;
  viewerUrl = URL.createObjectURL(state.blob);
  const image = viewer.querySelector("img");
  image.src = viewerUrl;
  image.alt = state.image.alt;
  viewer.querySelector(".chat-image-viewer-title").textContent = state.image.alt;
  document.body.classList.add("chat-image-viewer-open");
  viewer.showModal();
}

function release(state) {
  state.version += 1;
  state.controller?.abort();
  state.image.removeAttribute("src");
  state.image.hidden = true;
  state.button.disabled = true;
  state.status.hidden = false;
  if (state.url) URL.revokeObjectURL(state.url);
  state.url = "";
  state.blob = null;
}

async function load(state) {
  state.loading = true;
  activeRequests += 1;
  state.controller = new AbortController();
  const timer = setTimeout(() => state.controller.abort(), 20000);
  const version = state.version;
  const current = () => state.node.isConnected && state.visible && version === state.version;
  let received = false;
  try {
    const blob = await apiBlob(`/v1/agent-files/image-preview?path=${encodeURIComponent(state.node.dataset.imagePath)}`, state.controller.signal);
    if (!current()) return;
    received = true;
    if (blob.size > 5 * 1024 * 1024 || !["image/jpeg", "image/png", "image/webp"].includes(blob.type)) {
      throw new Error("unsupported image");
    }
    state.url = URL.createObjectURL(blob);
    state.image.src = state.url;
    await state.image.decode();
    if (!current()) return;
    const ratio = state.image.naturalWidth / state.image.naturalHeight;
    state.node.style.width = `min(100%, 420px, max(120px, clamp(${120 * ratio}px, ${25 * ratio}svh, ${240 * ratio}px)))`;
    state.blob = blob;
    state.image.hidden = false;
    state.status.hidden = true;
    state.button.disabled = false;
  } catch (error) {
    if (current()) {
      release(state);
      state.failed = true;
      state.node.classList.add("chat-image-failed");
      state.status.textContent = "Preview unavailable";
      state.retry.hidden = received || (error.status >= 400 && error.status < 500
        && ![408, 429].includes(error.status));
    }
  } finally {
    clearTimeout(timer);
    state.controller = null;
    state.loading = false;
    activeRequests -= 1;
    pump();
  }
}

function pump() {
  for (const state of states.values()) {
    if (activeRequests >= 2) break;
    if (state.visible && !state.loading && !state.blob && !state.failed) void load(state);
  }
}

const observer = new IntersectionObserver(entries => {
  for (const entry of entries) {
    const state = states.get(entry.target);
    if (!state) continue;
    state.visible = entry.isIntersecting;
    if (!state.visible) release(state);
  }
  pump();
}, { rootMargin: "300px" });

export function refreshChatImages(root) {
  // History entries are patched in place. Release removed entries and images
  // far from the viewport, keeping blob and decoded-image memory bounded by
  // the visible conversation instead of its entire retained history.
  for (const [node, state] of states) {
    if (root.contains(node)) continue;
    observer.unobserve(node);
    release(state);
    states.delete(node);
  }
  if (viewerOpener && !viewerOpener.isConnected) closeViewer();
  for (const node of root.querySelectorAll(".chat-image")) {
    if (states.has(node)) continue;
    const state = {
      node, button: node.querySelector(".chat-image-preview"),
      retry: node.querySelector(".chat-image-retry"),
      image: node.querySelector("img"), status: node.querySelector(".chat-image-status"),
      version: 0, visible: false, loading: false, failed: false, blob: null, url: "",
    };
    states.set(node, state);
    state.button.addEventListener("click", () => expandImage(state));
    state.retry.addEventListener("click", () => {
      state.failed = false;
      state.retry.hidden = true;
      state.status.textContent = "Loading preview…";
      state.node.classList.remove("chat-image-failed");
      pump();
    });
    observer.observe(node);
  }
}

export function clearChatImages() {
  closeViewer();
  for (const state of states.values()) release(state);
  observer.disconnect();
  states.clear();
}
