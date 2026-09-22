(() => {
"use strict";
if (window.KernDictation) return;
const SAMPLE_RATE = 16000;
const FINISH_MS = 5000;
let database;
function openDatabase() {
  if (!database) database = new Promise((resolve, reject) => {
    const request = indexedDB.open("kern-dictation", 1);
    request.onupgradeneeded = () => request.result.createObjectStore("audio", { keyPath: "id" });
    request.onsuccess = () => resolve(request.result);
    request.onblocked = request.onerror = () => reject(new Error("Allow browser storage, then retry dictation."));
  }).catch(cause => { database = null; throw cause; });
  return database;
}
async function storedAudio(action, value) {
  const db = await openDatabase();
  return new Promise((resolve, reject) => {
    const tx = db.transaction("audio", action === "getAll" ? "readonly" : "readwrite");
    const request = tx.objectStore("audio")[action](...(value === undefined ? [] : [value]));
    tx.oncomplete = () => resolve(request.result);
    tx.onerror = tx.onabort = () => reject(new Error("Could not save this recording. Free browser storage, then retry."));
  });
}
function base64(pcm) {
  const bytes = new Uint8Array(pcm.length * 2), view = new DataView(bytes.buffer);
  for (let i = 0; i < pcm.length; i++) view.setInt16(i * 2, pcm[i], true);
  let binary = "";
  for (let i = 0; i < bytes.length; i += 8192) binary += String.fromCharCode(...bytes.subarray(i, i + 8192));
  return btoa(binary);
}
async function checkReady() {
  const controller = new AbortController();
  const timeout = setTimeout(() => controller.abort(), 2000);
  try {
    const response = await fetch("/v1/dictation/ready", {
      credentials: "same-origin", signal: controller.signal,
      headers: { "X-Kern-Csrf": "1", "X-Kern-Session-Activity": "1" },
    });
    if (response.status === 401) throw new Error("Your session expired. Sign in again, then retry.");
    const body = await response.json();
    if (!response.ok || body.ready !== true) throw new Error(body.error?.message || "Transcription model isn't loaded yet. Click the mic to retry.");
  } catch (cause) {
    if (cause.name === "AbortError" || cause instanceof TypeError) throw new Error("Transcription is unavailable. Click the mic to retry.");
    throw cause;
  } finally { clearTimeout(timeout); }
}

function mount({ composer, textarea, scope, getKey, append, changed, unavailable }) {
  const mic = document.createElement("button");
  mic.type = "button"; mic.className = "attach-button dictation-mic";
  mic.innerHTML = '<svg viewBox="0 0 20 20" aria-hidden="true"><rect x="7" y="2" width="6" height="10" rx="3" fill="none" stroke="currentColor" stroke-width="1.6"/><path d="M4.5 9.5a5.5 5.5 0 0 0 11 0M10 15v3m-3 0h6" fill="none" stroke="currentColor" stroke-width="1.6" stroke-linecap="round"/></svg><span class="dictation-indicator" aria-hidden="true"></span>';
  composer.querySelector(".send-button").before(mic);
  const panel = document.createElement("div"); panel.className = "dictation-panel";
  const status = document.createElement("span"); status.className = "dictation-status"; status.setAttribute("role", "status");
  const clock = document.createElement("span"); clock.className = "dictation-time"; clock.ariaLabel = "Recording duration";
  const level = document.createElement("meter"); level.min = 0; level.max = 1; level.value = 0; level.ariaLabel = "Microphone level";
  const pause = button("Pause dictation", "Pause", () => stop(true));
  const finish = button("Stop dictation", "Done", () => stop(false));
  const discard = button("Discard pending recording", "Discard audio", async () => {
    const key = getKey();
    if (!confirm("Discard the untranscribed audio? Text already in your prompt is kept.")) return;
    cancelRun(key);
    for (const item of pending(key)) { item.discarded = true; inflight.get(item.id)?.controller.abort(); }
    await writes.catch(() => {});
    for (const item of pending(key)) await storedAudio("delete", item.id);
    queue = queue.filter(item => item.key !== key);
    errors.delete(key); render();
  });
  panel.append(level, status, clock, pause, finish, discard); textarea.after(panel);
  let queue = [], ready = false, opening = null, capture = null, stopping = null, job = null, ending = null;
  let sessionKey = null, paused = false, recording = false, duration = 0;
  let parts = [], samples = 0, speech = false, silence = 0, checkpointSamples = 0;
  let segmentId = null, segmentCreated = Date.now();
  let writes = Promise.resolve(), timer = null, lastBusy = null;
  const errors = new Map(), inflight = new Map();
  const supported = Boolean(navigator.mediaDevices?.getUserMedia && window.AudioWorkletNode && window.crypto?.randomUUID);
  function pending(key) { return queue.filter(item => item.key === key); }
  function visible() { return !document.hidden && composer.getClientRects().length > 0; }
  function busy() {
    const key = getKey();
    return !ready || opening?.key === key || stopping?.key === key || (recording && sessionKey === key) || pending(key).length > 0;
  }
  function button(label, text, action) {
    const element = document.createElement("button"); element.type = "button"; element.className = "dictation-action";
    element.ariaLabel = label; element.textContent = text;
    element.addEventListener("click", () => { const key = getKey(); Promise.resolve(action()).catch(cause => fail(key, cause)); });
    return element;
  }
  function render() {
    const key = getKey(), here = sessionKey === key;
    const listening = here && recording;
    const processing = job?.key === key || opening?.key === key || stopping?.key === key;
    const waiting = pending(key).length > 0;
    const message = errors.get(key) || (waiting && !processing && !listening ? "Some audio hasn't been transcribed. Click the mic to retry." : "");
    const state = listening ? "recording" : processing ? "processing" : message ? "warning" : "idle";
    const label = listening ? "Pause dictation" : processing ? "Transcribing" : waiting ? "Retry transcription" : message ? "Retry dictation" : here && paused ? "Resume dictation" : "Start dictation";
    mic.dataset.state = state; mic.ariaLabel = label;
    mic.title = !supported ? "Dictation needs microphone support on HTTPS." : message || label;
    mic.setAttribute("aria-pressed", String(listening));
    mic.disabled = !supported || !ready || unavailable() || processing;
    if (listening) mic.disabled = false;
    status.textContent = message;
    status.hidden = !message;
    level.hidden = !listening;
    clock.hidden = !here || (!listening && !paused && !stopping);
    const seconds = Math.floor(duration / SAMPLE_RATE);
    clock.textContent = `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, "0")}`;
    pause.hidden = !listening;
    finish.hidden = !listening && !(here && paused) && opening?.kind !== "record";
    discard.hidden = !waiting || processing || listening;
    panel.hidden = !message && !listening && !(here && paused) && opening?.kind !== "record";
    const nextBusy = busy();
    if (lastBusy !== nextBusy) { lastBusy = nextBusy; changed(); }
  }
  function persist(item) {
    writes = writes.catch(() => {}).then(async () => {
      if (!item.discarded) { await storedAudio("put", item); item.unsaved = false; }
    });
    return writes;
  }
  function currentSegment() {
    const pcm = new Int16Array(samples); let offset = 0;
    for (const part of parts) { pcm.set(part, offset); offset += part.length; }
    return { id: segmentId, scope, key: sessionKey, created: segmentCreated, pcm, unsaved: true };
  }
  function resetSegment() {
    parts = []; samples = 0; speech = false; silence = 0; checkpointSamples = 0;
    segmentId = crypto.randomUUID(); segmentCreated = Math.max(Date.now(), segmentCreated + 1);
  }
  function enqueue() {
    if (samples && speech) {
      const item = currentSegment(); queue.push(item);
      void persist(item).then(() => {
        if (!errors.has(item.key) && ((recording && sessionKey === item.key) || ending?.key === item.key)) drain(item.key);
      }).catch(cause => fail(item.key, cause));
    }
    resetSegment(); render();
  }
  function receive(pcm) {
    duration += pcm.length;
    let power = 0; for (const value of pcm) power += (value / 32768) ** 2;
    const rms = Math.sqrt(power / pcm.length); level.value = Math.min(1, rms * 12);
    parts.push(pcm); samples += pcm.length;
    if (rms > 0.008) { speech = true; silence = 0; } else silence += pcm.length;
    if (!speech && samples > SAMPLE_RATE * 0.3) samples -= parts.shift().length;
    if (speech && samples - checkpointSamples >= SAMPLE_RATE) {
      checkpointSamples = samples;
      const item = currentSegment(); void persist(item).catch(cause => fail(item.key, cause));
    }
    if (speech && ((silence >= SAMPLE_RATE * 0.7 && samples >= SAMPLE_RATE) || samples >= SAMPLE_RATE * 8)) enqueue();
    if (pending(sessionKey).reduce((n, item) => n + item.pcm.length, 0) >= SAMPLE_RATE * 60 && recording) void stop(true);
  }
  function cancelRun(key) {
    if (job?.key === key) { job.cancelled = true; job = null; }
    if (ending?.key === key) { clearTimeout(ending.timer); ending = null; }
  }
  function beginFinish(key) {
    if (ending?.key === key) return;
    const end = { key, until: Date.now() + FINISH_MS, timer: null };
    end.timer = setTimeout(() => {
      if (ending !== end) return;
      cancelRun(key);
      if (pending(key).length || samples) errors.set(key, "Some audio hasn't been transcribed. Click the mic to retry.");
      render();
    }, FINISH_MS);
    ending = end;
  }
  function active(run) {
    return job === run && !run.cancelled && (!ending || ending.key !== run.key || Date.now() < ending.until);
  }
  function fail(key, cause) {
    errors.set(key, cause.message || "Dictation stopped. Your prompt is kept.");
    cancelRun(key);
    if (sessionKey === key && recording) void stop(false, false);
    render();
  }
  async function closeResources(resources) {
    resources.stream?.getTracks().forEach(track => track.stop());
    resources.node?.disconnect();
    if (resources.context && resources.context.state !== "closed") await resources.context.close().catch(() => {});
  }
  async function start() {
    const key = getKey();
    if (recording) return stop(true);
    if (!supported || opening || stopping || unavailable() || !ready) return;
    if (pending(key).length) return retry(key);
    const attempt = { key, kind: "record", context: null, stream: null, node: null };
    opening = attempt; sessionKey = key; errors.delete(key); render();
    try {
      // Resume within the click gesture, but do not open the microphone until ready.
      attempt.context = new AudioContext();
      await Promise.all([attempt.context.resume(), checkReady(), openDatabase()]);
      if (opening !== attempt || !visible() || key !== getKey() || unavailable()) return;
      attempt.stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true }, video: false });
      if (opening !== attempt || !visible() || key !== getKey() || unavailable()) return;
      await attempt.context.audioWorklet.addModule("/workspace/dictation-worklet.js");
      if (opening !== attempt || !visible() || key !== getKey() || unavailable()) return;
      attempt.node = new AudioWorkletNode(attempt.context, "kern-dictation", { channelCount: 1, channelCountMode: "explicit" });
      attempt.node.port.onmessage = event => { if (capture === attempt && event.data.pcm) receive(event.data.pcm); };
      attempt.context.createMediaStreamSource(attempt.stream).connect(attempt.node);
      attempt.node.connect(attempt.context.destination);
      for (const track of attempt.stream.getTracks()) track.addEventListener("ended", () => {
        if (capture === attempt && recording) fail(key, new Error("Microphone disconnected. Reconnect it to retry."));
      });
      attempt.context.addEventListener("statechange", () => {
        if (capture === attempt && recording && attempt.context.state !== "running") fail(key, new Error("Recording paused by your device. Click the mic to retry."));
      });
      if (!paused) duration = 0;
      paused = false; resetSegment(); capture = attempt; recording = true;
      timer = setInterval(sync, 250);
    } catch (cause) {
      if (opening === attempt) errors.set(key, ({
        NotAllowedError: "Microphone access was denied. Allow it in your browser, then retry.",
        NotFoundError: "No microphone found. Connect one, then retry.",
        NotReadableError: "Your microphone is busy or unavailable. Check other recording apps, then retry.",
      })[cause.name] || cause.message);
    } finally {
      if (capture !== attempt) await closeResources(attempt);
      if (opening === attempt) opening = null;
      render();
    }
  }
  async function stop(keepPaused, finishPending = true) {
    if (opening) {
      const attempt = opening; opening = null;
      void closeResources(attempt);
    }
    if (stopping) return stopping.promise;
    paused = keepPaused;
    if (!recording) { render(); return; }
    const key = sessionKey, resources = capture;
    recording = false;
    if (finishPending) beginFinish(key); else cancelRun(key);
    clearInterval(timer); timer = null;
    const operation = { key, promise: null }; stopping = operation;
    operation.promise = (async () => {
      await new Promise(resolve => {
        const timeout = setTimeout(resolve, 500);
        resources.node.port.onmessage = event => {
          if (capture === resources && event.data.pcm) receive(event.data.pcm);
          if (event.data.stopped) { clearTimeout(timeout); resolve(); }
        };
        resources.node.port.postMessage("stop");
      });
      await closeResources(resources);
      capture = null; enqueue(); await writes.catch(() => {});
      if (stopping === operation) stopping = null;
      if (finishPending && ending?.key === key && !errors.has(key)) drain(key);
      settle(key); render();
    })();
    render(); return operation.promise;
  }
  function settle(key) {
    if (!pending(key).length && !recording && stopping?.key !== key) {
      if (ending?.key === key) { clearTimeout(ending.timer); ending = null; }
    }
  }
  function textFor(item) {
    if (item.text !== undefined) return Promise.resolve(item.text);
    if (inflight.has(item.id)) return inflight.get(item.id).promise;
    const controller = new AbortController();
    const request = { controller, promise: null };
    request.promise = (async () => {
      const timeout = setTimeout(() => controller.abort(), 70000);
      try {
        const response = await fetch("/v1/dictation/transcribe", {
          method: "POST", credentials: "same-origin", signal: controller.signal,
          headers: { "Content-Type": "application/json", "X-Kern-Csrf": "1", "X-Kern-Session-Activity": "1" },
          body: JSON.stringify({ audio: base64(item.pcm) }),
        });
        if (response.status === 401) throw new Error("Your session expired. Sign in again, then retry the saved audio.");
        let body;
        try { body = await response.json(); } catch { throw new Error("The host returned an unreadable response. Click the mic to retry."); }
        if (!response.ok || typeof body.text !== "string") throw new Error(body.error?.message || "Transcription is unavailable. Click the mic to retry.");
        if (!item.discarded) { item.text = body.text; await persist(item); }
        return body.text;
      } catch (cause) {
        if (cause.name === "AbortError") throw new Error("Transcription timed out. Click the mic to retry the saved audio.");
        if (cause instanceof TypeError) throw new Error("Connection lost. Reconnect, then click the mic to retry.");
        throw cause;
      } finally { clearTimeout(timeout); inflight.delete(item.id); }
    })();
    inflight.set(item.id, request); return request.promise;
  }
  function drain(key) {
    if (job || errors.has(key)) return;
    const run = { key, cancelled: false }; job = run; render();
    void (async () => {
      try {
        while (pending(key).length && active(run)) {
          await writes.catch(() => {});
          if (!active(run)) break;
          const item = pending(key)[0];
          if (item.unsaved) await persist(item);
          const text = await textFor(item);
          if (!active(run) || item.discarded) break;
          const forgetReceipt = append(item.key, text, item.id);
          await storedAudio("delete", item.id);
          forgetReceipt?.();
          queue = queue.filter(other => other.id !== item.id);
        }
      } catch (cause) { if (active(run)) fail(key, cause); }
      finally {
        if (job === run) job = null;
        settle(key); render();
      }
    })();
  }
  async function retry(key) {
    if (job || opening || stopping) return;
    const attempt = { key, kind: "retry" }; opening = attempt; sessionKey = key; errors.delete(key);
    beginFinish(key); render();
    try {
      // Completed late results need no new host request.
      if (pending(key).some(item => item.text === undefined && !inflight.has(item.id))) await checkReady();
      if (opening === attempt && getKey() === key && visible() && ending?.key === key) drain(key);
    } catch (cause) { if (opening === attempt) fail(key, cause); }
    finally { if (opening === attempt) opening = null; render(); }
  }
  function sync() {
    if ((opening || recording || job || stopping) && (!visible() || sessionKey !== getKey() || unavailable())) {
      const key = sessionKey;
      cancelRun(key);
      if (opening || recording) void stop(false, false);
    }
    if (sessionKey !== getKey() && !recording) paused = false;
    render();
  }
  const controller = { busy, sync, stop };
  mic.addEventListener("click", () => void start());
  window.addEventListener("hashchange", sync);
  document.addEventListener("visibilitychange", sync);
  window.addEventListener("pagehide", () => { cancelRun(sessionKey); void stop(false, false); });
  window.addEventListener("beforeunload", event => {
    if (recording || opening || stopping || queue.length) { event.preventDefault(); event.returnValue = ""; }
  });
  void storedAudio("getAll").then(items => {
    queue = items.filter(item => item.scope === scope).map(item => ({ ...item, unsaved: false, discarded: false })).sort((a, b) => a.created - b.created);
  }).catch(cause => errors.set(getKey(), cause.message)).finally(() => { ready = true; render(); });
  render(); return controller;
}

// One active Kern tab per browser is assumed. Persist the phrase receipt with
// the draft before deleting audio, so a reload/Retry cannot insert it twice.
function appendDraft({ drafts, storageKey, key, currentKey, textarea, updated }, text, id) {
  const receipts = drafts.__dictationReceipts || [];
  const forgetReceipt = () => {
    // Only remove the receipt after deleting audio. A failed cleanup is harmless;
    // retaining a receipt is preferable to risking a duplicate after a reload.
    const next = { ...drafts, __dictationReceipts: (drafts.__dictationReceipts || []).filter(value => value !== id) };
    try { localStorage.setItem(storageKey, JSON.stringify(next)); Object.assign(drafts, next); } catch {}
  };
  if (receipts.includes(id)) return forgetReceipt;
  const selected = key === currentKey(), before = selected ? textarea.value : drafts[key] || "";
  const suffix = text ? `${before && !/\s$/.test(before) ? " " : ""}${text}` : "";
  const next = { ...drafts, [key]: before + suffix, __dictationReceipts: [...receipts, id] };
  try { localStorage.setItem(storageKey, JSON.stringify(next)); }
  catch { throw new Error("Could not save the transcript. Free browser storage, then click the mic to retry."); }
  Object.assign(drafts, next);
  if (selected && suffix) {
    const start = textarea.selectionStart, end = textarea.selectionEnd;
    const atEnd = start === before.length && end === before.length;
    textarea.value = next[key]; textarea.setSelectionRange(atEnd ? textarea.value.length : start, atEnd ? textarea.value.length : end);
    updated(); if (atEnd) textarea.scrollTop = textarea.scrollHeight;
  }
  return forgetReceipt;
}
window.KernDictation = { mount, appendDraft };
})();
