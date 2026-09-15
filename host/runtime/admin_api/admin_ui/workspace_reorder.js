// Pointer events support mouse, pen, and touch with the same drag handle.
export function createWorkspaceReorder({ move, render, reportError }) {
  let drag = null;
  let saving = false;
  const status = document.createElement("div");
  status.className = "workspace-reorder-status";
  status.setAttribute("role", "status");
  document.body.append(status);

  function focusHandle(container, id) {
    container.querySelector(`[data-reorder-id="${CSS.escape(id)}"]`)?.focus({ preventScroll: true });
  }

  async function save(container, kind, id, beforeId, label) {
    saving = true;
    container.setAttribute("aria-busy", "true");
    try {
      await move(kind, id, beforeId);
      status.textContent = `Moved ${label}.`;
    } catch (error) {
      reportError(error.message);
    } finally {
      saving = false;
      container.removeAttribute("aria-busy");
      render();
      focusHandle(container, id);
    }
  }

  function clearIndicators(container) {
    for (const row of container.children) {
      row.classList.remove("workspace-reorder-before", "workspace-reorder-after");
    }
  }

  function updateTarget() {
    if (!drag?.started) return;
    const { container, row, y } = drag;
    clearIndicators(container);
    const others = [...container.children].filter(candidate => candidate !== row);
    const target = others.find(candidate => {
      const box = candidate.getBoundingClientRect();
      return y < box.top + box.height / 2;
    });
    drag.beforeId = target?.dataset.reorderRowId || null;
    if (target) target.classList.add("workspace-reorder-before");
    else others.at(-1)?.classList.add("workspace-reorder-after");
  }

  function scrollFrame() {
    if (!drag?.started) return;
    const box = drag.scroller.getBoundingClientRect();
    const top = Math.max(0, box.top);
    const bottom = Math.min(window.innerHeight, box.bottom);
    if (drag.y < top + 36) drag.scroller.scrollTop -= 10;
    else if (drag.y > bottom - 36) drag.scroller.scrollTop += 10;
    updateTarget();
    drag.frame = requestAnimationFrame(scrollFrame);
  }

  function finish(cancelled) {
    if (!drag) return;
    const current = drag;
    drag = null;
    cancelAnimationFrame(current.frame);
    current.row.classList.remove("workspace-reorder-dragging");
    clearIndicators(current.container);
    if (current.handle.hasPointerCapture(current.pointerId)) {
      current.handle.releasePointerCapture(current.pointerId);
    }
    if (!cancelled && current.started && current.beforeId !== current.originalBeforeId) {
      void save(current.container, current.kind, current.id, current.beforeId, current.label);
    } else {
      render();
      focusHandle(current.container, current.id);
    }
  }

  window.addEventListener("blur", () => finish(true));
  window.addEventListener("keydown", event => {
    if (event.key === "Escape" && drag) {
      event.preventDefault();
      event.stopPropagation();
      finish(true);
    }
  }, { capture: true });

  function addHandle(container, row, kind, id, label, disabled) {
    row.dataset.reorderRowId = id;
    row.classList.add("workspace-nav-reorderable");
    const handle = document.createElement("button");
    handle.type = "button";
    handle.className = "workspace-reorder-handle";
    handle.dataset.reorderId = id;
    handle.disabled = disabled;
    handle.textContent = "≡";
    handle.title = `Reorder ${label}: drag or use Up and Down arrow keys`;
    handle.setAttribute("aria-label", handle.title);
    handle.setAttribute("aria-keyshortcuts", "ArrowUp ArrowDown");
    handle.addEventListener("click", event => event.stopPropagation());
    handle.addEventListener("keydown", event => {
      if (!["ArrowUp", "ArrowDown"].includes(event.key)) return;
      event.preventDefault();
      event.stopPropagation();
      if (saving || drag) return;
      const rows = [...container.children];
      const index = rows.indexOf(row);
      const up = event.key === "ArrowUp";
      if ((up && index === 0) || (!up && index === rows.length - 1)) return;
      const beforeId = rows[up ? index - 1 : index + 2]?.dataset.reorderRowId || null;
      void save(container, kind, id, beforeId, label);
    });
    handle.addEventListener("pointerdown", event => {
      if (saving || drag || event.button !== 0 || !event.isPrimary) return;
      event.preventDefault();
      handle.focus({ preventScroll: true });
      handle.setPointerCapture(event.pointerId);
      let scroller = container.parentElement;
      while (scroller.parentElement && scroller.scrollHeight <= scroller.clientHeight) {
        scroller = scroller.parentElement;
      }
      const beforeId = row.nextElementSibling?.dataset.reorderRowId || null;
      drag = {
        container, row, handle, kind, id, label, scroller,
        pointerId: event.pointerId, startY: event.clientY, y: event.clientY,
        beforeId, originalBeforeId: beforeId, started: false, frame: 0,
      };
    });
    handle.addEventListener("pointermove", event => {
      if (!drag || drag.pointerId !== event.pointerId) return;
      drag.y = event.clientY;
      if (!drag.started && Math.abs(drag.y - drag.startY) >= 5) {
        drag.started = true;
        row.classList.add("workspace-reorder-dragging");
        scrollFrame();
      }
      updateTarget();
    });
    handle.addEventListener("pointerup", event => {
      if (drag?.pointerId === event.pointerId) finish(false);
    });
    for (const name of ["pointercancel", "lostpointercapture"]) {
      handle.addEventListener(name, event => {
        if (drag?.pointerId === event.pointerId) finish(true);
      });
    }
    row.prepend(handle);
  }

  return { addHandle, get busy() { return Boolean(drag) || saving; } };
}
