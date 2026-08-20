// First-deploy checklist. Every step is derived from live host state by the
// Workspace, so all four are read the same way and none of them go stale.

import { api } from "./api.js";
import { $, esc, setHtml } from "./helpers.js";

export const STARTER_PROMPTS = {
  chat: "Give me a quick tour of this Kern host and suggest three useful things we can do together.",
  app: "Create a personal dashboard app that tracks my weekly priorities, with status, due date, and a simple progress summary.",
  schedule: "Create a daily 09:00 UTC schedule that harvests durable facts from my past day threads into Memory and reorganizes existing pages so they stay accurate and concise.",
};

let workspaceStatus = null;

function stepRow({ key, number, title, description, example, complete, actions }) {
  return `<div class="getting-started-step${complete ? " complete" : ""}" data-getting-started-step="${esc(key)}">
    <span class="getting-started-check" aria-hidden="true">${complete ? "✓" : esc(number)}</span>
    <span class="getting-started-step-copy">
      <strong>${esc(title)}</strong>
      <span>${esc(description)}</span>
      ${complete || !example ? "" : `<span class="getting-started-example"><span>Try asking</span>${esc(example)}</span>`}
    </span>
    <span class="getting-started-actions">
      ${complete ? '<span class="getting-started-done">Done</span>' : actions}
    </span>
  </div>`;
}

function promptAction(step, label = "Use this prompt") {
  return `<button class="ghost sm" data-action="getting-started-prompt" data-step="${esc(step)}">${esc(label)}</button>`;
}

export function renderGettingStarted() {
  const root = $("getting-started");
  if (!root) return;
  if (!workspaceStatus || workspaceStatus.dismissed === true) {
    root.hidden = true;
    return;
  }

  const steps = [
    {
      key: "provider",
      number: 1,
      title: "Connect an AI provider",
      description: "Activate at least one inference provider so your agent can work.",
      complete: workspaceStatus.provider_ready === true,
      actions: `<span class="getting-started-provider-actions" aria-label="Choose an AI provider">
        <button class="ghost sm" data-action="open-home-integration" data-guide="openai">OpenAI</button>
        <button class="ghost sm" data-action="open-home-integration" data-guide="claude">Claude</button>
        <button class="ghost sm" data-action="open-home-integration" data-guide="bedrock">Bedrock</button>
      </span>`,
    },
    {
      key: "chat",
      number: 2,
      title: "Start your first chat",
      description: "Give your agent a real task. A new thread is saved after you send the message.",
      example: STARTER_PROMPTS.chat,
      complete: workspaceStatus.chat_created === true,
      actions: promptAction("chat", "Start a chat"),
    },
    {
      key: "app",
      number: 3,
      title: "Ask your agent to create an app",
      description: "Turn a recurring workflow or dataset into a focused personal interface.",
      example: STARTER_PROMPTS.app,
      complete: workspaceStatus.app_created === true,
      actions: promptAction("app", "Ask agent"),
    },
    {
      key: "schedule",
      number: 4,
      title: "Ask your agent to create a schedule",
      description: "Automate recurring work with an interval or daily schedule.",
      example: STARTER_PROMPTS.schedule,
      complete: workspaceStatus.schedule_created === true,
      actions: promptAction("schedule", "Ask agent"),
    },
  ];
  const completed = steps.filter(step => step.complete).length;
  const allComplete = completed === steps.length;
  const progressLabel = `${completed} of ${steps.length} complete`;
  setHtml(root, `
    <div class="getting-started-head">
      <div>
        <p class="getting-started-eyebrow">Get started</p>
        <h2 id="getting-started-title">${allComplete ? "You've explored every feature" : "Explore what Kern can do"}</h2>
        <p>${allComplete ? "Chat, Apps, and Schedules are all in play. Keep building on them from the sidebar." : "Four short tasks walk you through each of Kern's core features."}</p>
      </div>
      <div class="getting-started-progress-copy"><strong>${esc(completed)}</strong><span>/ ${esc(steps.length)}</span></div>
    </div>
    <progress class="getting-started-progress" aria-label="${esc(progressLabel)}" max="${esc(steps.length)}" value="${esc(completed)}"></progress>
    <div class="getting-started-steps">${steps.map(stepRow).join("")}</div>
    <div class="getting-started-finish">
      <span>${allComplete ? "Nice work. You can return to Apps and Schedules from the sidebar anytime." : "Dismiss this whenever you like."}</span>
      <button class="ghost sm" data-action="dismiss-getting-started">Dismiss checklist</button>
    </div>
  `);
  root.hidden = false;
}

export async function refreshGettingStarted() {
  // Re-read every time so an authenticated host replacement in an already-open
  // admin tab cannot inherit the previous host's setup state.
  workspaceStatus = await api("GET", "/v1/workspace/getting-started");
  renderGettingStarted();
}

export async function dismissGettingStarted() {
  // The host owns the decision, so every operator browser honors it. Hide the
  // panel first: the record is durable, and waiting on the round trip would
  // leave the button looking dead.
  const root = $("getting-started");
  if (root) root.hidden = true;
  workspaceStatus = await api("POST", "/v1/workspace/getting-started/dismiss");
  renderGettingStarted();
}
