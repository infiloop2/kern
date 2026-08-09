"use strict";

let generatedWorker = null;

globalThis.addEventListener("message", event => {
  const message = event.data;
  if (!message || typeof message !== "object") return;
  if (message.type === "create") {
    if (typeof message.source !== "string") return;
    terminateGeneratedWorker();
    let worker;
    try {
      worker = new Worker(
        `data:application/javascript;charset=utf-8,${encodeURIComponent(message.source)}`,
      );
    } catch (_error) {
      globalThis.postMessage({ type: "capability-worker-error", reason: "worker-create" });
      return;
    }
    generatedWorker = worker;
    worker.addEventListener("message", workerEvent => {
      if (generatedWorker !== worker) return;
      globalThis.postMessage({
        type: "capability-worker-message",
        data: workerEvent.data,
      });
    });
    worker.addEventListener("error", errorEvent => {
      errorEvent.preventDefault();
      if (generatedWorker !== worker) return;
      globalThis.postMessage({ type: "capability-worker-error", reason: "worker-runtime" });
      terminateGeneratedWorker();
    });
    return;
  }
  if (message.type === "worker-post") {
    generatedWorker?.postMessage(message.data);
    return;
  }
  if (message.type === "terminate") terminateGeneratedWorker();
});

function terminateGeneratedWorker() {
  if (!generatedWorker) return;
  generatedWorker.terminate();
  generatedWorker = null;
}
