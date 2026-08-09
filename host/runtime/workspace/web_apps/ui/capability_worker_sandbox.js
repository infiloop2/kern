"use strict";

const workers = new Map();

window.addEventListener("message", event => {
  if (event.source !== parent || !event.data || typeof event.data !== "object") return;
  const message = event.data;
  if (message.type === "create") {
    if (typeof message.worker_id !== "string" || typeof message.source !== "string") return;
    terminate(message.worker_id);
    const url = URL.createObjectURL(new Blob([message.source], { type: "application/javascript" }));
    const worker = new Worker(url);
    URL.revokeObjectURL(url);
    workers.set(message.worker_id, worker);
    worker.addEventListener("message", workerEvent => {
      parent.postMessage({
        type: "capability-worker-message",
        worker_id: message.worker_id,
        data: workerEvent.data,
      }, "*");
    });
    worker.addEventListener("error", () => {
      parent.postMessage({
        type: "capability-worker-error",
        worker_id: message.worker_id,
      }, "*");
      terminate(message.worker_id);
    });
    return;
  }
  if (message.type === "worker-post") {
    workers.get(message.worker_id)?.postMessage(message.data);
    return;
  }
  if (message.type === "terminate") terminate(message.worker_id);
});

function terminate(workerId) {
  const worker = workers.get(workerId);
  if (!worker) return;
  workers.delete(workerId);
  worker.terminate();
}

parent.postMessage({ type: "capability-sandbox-ready" }, "*");
