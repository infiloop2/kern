import { api, setUnauthorizedHandler } from "./api.js";
const $ = id => document.getElementById(id);
const params = new URLSearchParams(location.search);
const reference = params.has("login") ? {login_id: params.get("login")} : {account_id: params.get("account")};
let lease = "", stopped = false, timer = null, queue = Promise.resolve();
const screen = $("browser-screen");
function message(text) { $("browser-message").textContent = text; }
function frameError(text) { const node = $("browser-frame-error"); node.textContent = text; node.hidden = !text; }
screen.onload = () => frameError("");
screen.onerror = () => frameError("The browser image could not be loaded. Reload the page or reopen this window.");
setUnauthorizedHandler(() => { stopped = true; message("Your Kern login expired. Sign in to Kern again, then reopen this window."); });
function call(operation, payload = {}) { return api("POST", `/v1/browser/${operation}`, {...reference, lease, ...payload}); }
function enqueue(task) {
  queue = queue.then(task).catch(error => { message(error.message); });
  return queue;
}
async function frame() {
  if (stopped || !lease) return;
  let result;
  try { result = await call("frame"); }
  catch (error) {
    frameError(error.message);
    if (error.message.includes("control expired")) { stopped = true; screen.removeAttribute("src"); screen.hidden = true; }
    return;
  }
  $("browser-origin").textContent = result.origin || "X browser";
  screen.src = `data:image/jpeg;base64,${result.image}`;
  screen.hidden = false;
}
function scheduleFrame() {
  if (stopped) return;
  timer = setTimeout(() => enqueue(frame).then(scheduleFrame), 1200);
}
function input(payload) { return enqueue(async () => { if (!stopped && lease) { await call("input", payload); } }); }
async function finish(operation) {
  stopped = true;
  clearTimeout(timer);
  try {
    await call(operation);
    lease = "";
    screen.removeAttribute("src"); screen.hidden = true;
    message(operation === "save" ? "Account connected. You can close this window." : "Browser closed. Check login to resume account actions.");
    window.opener?.postMessage({type: "kern-browser-saved"}, location.origin);
    window.close();
  } catch (error) {
    stopped = false; message(error.message); scheduleFrame();
  }
}
$("browser-save").onclick = () => enqueue(() => finish("save"));
$("browser-cancel").onclick = () => enqueue(() => finish("cancel"));
$("browser-home").onclick = () => input({kind: "home"});
$("browser-refresh").onclick = () => input({kind: "reload"});
$("browser-tab").onclick = () => input({kind: "key", key: "Tab"});
$("browser-enter").onclick = () => input({kind: "key", key: "Enter"});
$("browser-insert").onclick = () => { const text = $("browser-text").value; $("browser-text").value = ""; if (text) input({kind: "text", text}); };
let dragging = false, lastMove = 0;
function pointer(event, phase) {
  const box = screen.getBoundingClientRect();
  input({kind: "pointer", phase, x: Math.min(1099, Math.max(0, Math.floor((event.clientX - box.left) * 1100 / box.width))), y: Math.min(759, Math.max(0, Math.floor((event.clientY - box.top) * 760 / box.height)))});
}
screen.onpointerdown = event => {
  if (event.button !== 0) return;
  screen.focus(); dragging = true; screen.setPointerCapture(event.pointerId); pointer(event, "down");
};
screen.onpointermove = event => { if (dragging && Date.now() - lastMove > 50) { lastMove = Date.now(); pointer(event, "move"); } };
function release(event) { if (dragging) { dragging = false; pointer(event, "up"); } }
screen.onpointerup = release;
screen.onpointercancel = release;
screen.onkeydown = event => {
  if (event.isComposing) return;
  if ((event.ctrlKey || event.metaKey) && event.key.toLowerCase() === "v") return;
  let key = event.key;
  if ((event.ctrlKey || event.metaKey) && key.toLowerCase() === "a") key = "Control+a";
  else if (event.ctrlKey || event.metaKey || event.altKey) return;
  else if (key === "Tab" && event.shiftKey) key = "Shift+Tab";
  if (["Enter", "Tab", "Shift+Tab", "Backspace", "Delete", "Escape", "ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown", "Home", "End", "Control+a"].includes(key)) {
    event.preventDefault(); input({kind: "key", key});
  } else if ([...key].length === 1) { event.preventDefault(); input({kind: "text", text: key}); }
};
screen.onpaste = event => { event.preventDefault(); const text = event.clipboardData.getData("text/plain"); if (text) input({kind: "text", text}); };
screen.addEventListener("wheel", event => { event.preventDefault(); input({kind: "scroll", delta: Math.max(-1000, Math.min(1000, Math.round(event.deltaY)))}); }, {passive: false});
window.addEventListener("pagehide", () => {
  stopped = true; clearTimeout(timer);
  if (lease) fetch("/v1/browser/cancel", {method: "POST", headers: {"Content-Type": "application/json", "X-Kern-Csrf": "1"}, body: JSON.stringify({...reference, lease}), keepalive: true}).catch(() => {});
});
try {
  const state = await api("POST", "/v1/browser/open", {...reference});
  lease = state.lease;
  $("browser-title").textContent = new URL(state.site).hostname;
  message("You have control. Sign in, then choose Save and close.");
  await frame(); scheduleFrame();
} catch (error) { message(error.message); }
