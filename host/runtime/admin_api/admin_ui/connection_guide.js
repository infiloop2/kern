// Full operator documentation for every managed integration, bundled tool,
// and custom domain access. Managed content comes from integration_catalog;
// bundled-tool content comes from the same manifests that drive execution.

import { api } from "./api.js";
import { $, esc, inlineCode, setHtml } from "./helpers.js";
import { CUSTOM_DOMAIN_GUIDE, MANAGED_INTEGRATIONS } from "./integration_catalog.js";

let selectedGuideId = "openai";
let loadedGuides = [];
let copyFeedbackTimer = null;
let copyFeedbackGeneration = 0;

const INTEGRATION_LOGOS = {
  openai: `<svg viewBox="0 0 512 512"><path fill="currentColor" d="M196.4 185.8v-48.6c0-4.1 1.5-7.2 5.1-9.2l97.8-56.3c13.3-7.7 29.2-11.3 45.6-11.3 61.4 0 100.4 47.6 100.4 98.3 0 3.6 0 7.7-.5 11.8l-101.5-59.4c-6.1-3.6-12.3-3.6-18.4 0l-128.5 74.7Zm228.3 189.4V259c0-7.2-3.1-12.3-9.2-15.9L287 168.4l42-24.1c3.6-2 6.7-2 10.2 0l97.8 56.4c28.2 16.4 47.1 51.2 47.1 85 0 38.9-23 74.8-59.4 89.5ZM166.2 272.8l-42-24.6c-3.6-2-5.1-5.1-5.1-9.2V126.4c0-54.8 42-96.3 98.8-96.3 21.5 0 41.5 7.2 58.4 20l-100.9 58.4c-6.1 3.6-9.2 8.7-9.2 15.9v148.4Zm90.4 52.2-60.2-33.8v-71.7l60.2-33.8 60.2 33.8v71.7L256.6 325Zm38.7 155.7c-21.5 0-41.5-7.2-58.4-20l100.9-58.4c6.1-3.6 9.2-8.7 9.2-15.9V237.9l42.5 24.6c3.6 2 5.1 5.1 5.1 9.2v112.6c0 54.8-42.5 96.3-99.3 96.4Zm-121.5-114.2-97.7-56.3C47.9 293.8 29 259 29 225.2c0-39.4 23.6-74.8 59.9-89.6v116.7c0 7.2 3.1 12.3 9.2 15.9l128 74.2-42 24.1c-3.6 2-6.7 2-10.3 0Zm-5.6 84c-57.9 0-100.4-43.5-100.4-97.3 0-4.1.5-8.2 1-12.3l100.9 58.4c6.1 3.6 12.3 3.6 18.4 0l128.5-74.2v48.6c0 4.1-1.5 7.2-5.1 9.2l-97.8 56.3c-13.3 7.7-29.2 11.3-45.5 11.3Zm127.1 60.9c62 0 113.7-44 125.4-102.4 57.3-14.9 94.2-68.6 94.2-123.4 0-35.8-15.4-70.7-43-95.7 2.6-10.8 4.1-21.5 4.1-32.3 0-73.2-59.4-128-128-128-13.8 0-27.1 2-40.4 6.7-23-22.5-54.8-36.9-89.6-36.9-62 0-113.7 44-125.4 102.4-57.3 14.8-94.2 68.6-94.2 123.4 0 35.8 15.4 70.7 43 95.7-2.6 10.8-4.1 21.5-4.1 32.3 0 73.2 59.4 128 128 128 13.8 0 27.1-2 40.4-6.7 23 22.5 54.8 36.9 89.6 36.9Z"/></svg>`,
  claude: `<svg viewBox="0 0 32 32"><path fill="currentColor" d="M14.1 3.3h3.8l-.5 9.3 7.8-5.1 1.9 3.3-8.3 4.2 8.3 4.2-1.9 3.3-7.8-5.1.5 9.3h-3.8l.5-9.3-7.8 5.1-1.9-3.3 8.3-4.2-8.3-4.2 1.9-3.3 7.8 5.1-.5-9.3Z"/></svg>`,
  bedrock: `<span class="integration-logo-word integration-logo-word-aws">aws</span><svg class="integration-logo-smile" viewBox="0 0 32 10"><path d="M4 2.5c6.5 4.8 14.8 5.1 23.8.2"/><path d="m24.2 1 4.2 1.4-2 3.8"/></svg>`,
  github: `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M12 .7A11.5 11.5 0 0 0 8.4 23c.6.1.8-.3.8-.6v-2.2c-3.4.7-4.1-1.4-4.1-1.4-.5-1.4-1.3-1.8-1.3-1.8-1.1-.7.1-.7.1-.7 1.2.1 1.8 1.2 1.8 1.2 1.1 1.8 2.8 1.3 3.5 1 .1-.8.4-1.3.8-1.6-2.7-.3-5.5-1.3-5.5-5.9 0-1.3.5-2.4 1.2-3.2-.1-.3-.5-1.5.1-3.1 0 0 1-.3 3.2 1.2a11 11 0 0 1 5.8 0c2.2-1.5 3.2-1.2 3.2-1.2.6 1.6.2 2.8.1 3.1.8.8 1.2 1.9 1.2 3.2 0 4.6-2.8 5.6-5.5 5.9.4.4.8 1.1.8 2.2v3.3c0 .3.2.7.8.6A11.5 11.5 0 0 0 12 .7Z"/></svg>`,
  python_packages: `<span class="integration-logo-word integration-logo-word-python"><b>Py</b></span>`,
  npm_packages: `<span class="integration-logo-word integration-logo-word-npm">npm</span>`,
  "tool:apify": `<span class="integration-logo-word integration-logo-word-apify">A</span>`,
  "tool:brave_search": `<svg viewBox="0 0 32 32"><path fill="none" stroke="currentColor" stroke-width="2.2" d="m16 3 10 4.2-1 14.2L16 28l-9-6.6L6 7.2 16 3Z"/><path fill="currentColor" d="M11 8.8h7c4 0 5.2 5 2.1 6.5 3.8 1.3 2.5 7.7-2 7.7H11V8.8Zm4 3v2.4h2.7c1.6 0 1.6-2.4 0-2.4H15Zm0 5.2v3h3c1.9 0 1.9-3 0-3h-3Z"/></svg>`,
  "tool:gmail": `<svg viewBox="0 0 32 32"><path class="gmail-blue" d="M4 10v15h5V14.3Z"/><path class="gmail-red" d="M4 10 8 7l8 6.2L24 7l4 3v15h-5V14.2L16 20 9 14.3V25H4Z"/><path class="gmail-yellow" d="m24 7 4 3-5 4.2V8Z"/><path class="gmail-green" d="M23 14.2 28 10v15h-5Z"/></svg>`,
  "tool:google_calendar": `<svg viewBox="0 0 32 32"><path class="calendar-blue" d="M6 5h20v22H6z"/><path class="calendar-green" d="M6 5h14v7H6z"/><path class="calendar-yellow" d="M6 12h7v15H6z"/><path class="calendar-red" d="M20 5h6v7h-6z"/><path fill="#fff" d="M13 14h6.3c3.1 0 4.7 1.6 4.7 3.7 0 1.5-.9 2.7-2.3 3.1v.1c1.7.3 2.7 1.5 2.7 3.2 0 .5-.1 1-.2 1.4H20c.2-.4.3-.8.3-1.3 0-1.3-.9-2.1-2.5-2.1h-1.5v-2.7h1.4c1.4 0 2.2-.7 2.2-1.8 0-1-.8-1.7-2.1-1.7H13V14Z"/></svg>`,
  "tool:ibkr": `<svg viewBox="0 0 775 1511"><path fill="currentColor" d="M.3 1510.2V775.3l668 734.9Z"/><circle cx="574.2" cy="954.4" r="200.2" fill="currentColor"/><path fill="currentColor" d="M668.3.4.3 1510.2V775.3Z"/></svg>`,
  "tool:instagram": `<svg viewBox="0 0 448 512"><path fill="currentColor" d="M224.3 141a115 115 0 1 0-.6 230 115 115 0 1 0 .6-230Zm-.6 40.4a74.6 74.6 0 1 1 .6 149.2 74.6 74.6 0 1 1-.6-149.2Zm93.4-45.1a26.8 26.8 0 1 1 53.6 0 26.8 26.8 0 1 1-53.6 0Zm129.7 27.2c-1.7-35.9-9.9-67.7-36.2-93.9-26.2-26.2-58-34.4-93.9-36.2-37-2.1-147.9-2.1-184.9 0-35.8 1.7-67.6 9.9-93.9 36.1S3.5 127.5 1.7 163.4c-2.1 37-2.1 147.9 0 184.9 1.7 35.9 9.9 67.7 36.2 93.9s58 34.4 93.9 36.2c37 2.1 147.9 2.1 184.9 0 35.9-1.7 67.7-9.9 93.9-36.2 26.2-26.2 34.4-58 36.2-93.9 2.1-37 2.1-147.8 0-184.8ZM399 388c-7.8 19.6-22.9 34.7-42.6 42.6-29.5 11.7-99.5 9-132.1 9s-102.7 2.6-132.1-9c-19.6-7.8-34.7-22.9-42.6-42.6-11.7-29.5-9-99.5-9-132.1s-2.6-102.7 9-132.1c7.8-19.6 22.9-34.7 42.6-42.6 29.5-11.7 99.5-9 132.1-9s102.7-2.6 132.1 9c19.6 7.8 34.7 22.9 42.6 42.6 11.7 29.5 9 99.5 9 132.1s2.7 102.7-9 132.1Z"/></svg>`,
  "tool:instagram_discovery": `<svg viewBox="0 0 32 32"><rect x="4" y="4" width="20" height="20" rx="6" fill="none" stroke="currentColor" stroke-width="2.4"/><circle cx="14" cy="14" r="4.5" fill="none" stroke="currentColor" stroke-width="2.4"/><circle cx="21" cy="7.8" r="1.4" fill="currentColor"/><circle cx="23.5" cy="23.5" r="4.5" fill="#111722" stroke="#fff" stroke-width="2"/><path d="m27 27 3 3" stroke="#fff" stroke-width="2.5" stroke-linecap="round"/></svg>`,
  "tool:linkedin": `<span class="integration-logo-word integration-logo-word-linkedin">in</span>`,
  "tool:linkedin_discovery": `<span class="integration-logo-word integration-logo-word-linkedin">in</span><svg class="integration-logo-search" viewBox="0 0 20 20"><circle cx="8" cy="8" r="5"/><path d="m12 12 5 5"/></svg>`,
  "tool:polymarket": `<svg viewBox="0 0 32 32"><path fill="none" stroke="currentColor" stroke-width="2.2" d="m7 8 18-4v20L7 28V8Z"/><path fill="currentColor" d="m11 11 9-2-4.2 7.2L20 22l-9 2V11Z"/></svg>`,
  "tool:reddit": `<svg viewBox="0 0 32 32"><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" d="m18 9 1.4-5 4.4 1.1"/><circle cx="25.3" cy="5.5" r="2" fill="none" stroke="currentColor" stroke-width="2"/><ellipse cx="16" cy="18" rx="10.5" ry="8" fill="none" stroke="currentColor" stroke-width="2"/><path fill="currentColor" d="M12.5 16.5a1.8 1.8 0 1 1-3.6 0 1.8 1.8 0 0 1 3.6 0Zm10.6 0a1.8 1.8 0 1 1-3.6 0 1.8 1.8 0 0 1 3.6 0Z"/><path fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" d="M11.5 21c2.6 2 6.4 2 9 0"/></svg>`,
  "tool:runway": `<span class="integration-logo-word integration-logo-word-runway">R</span>`,
  "tool:seedance": `<svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="11" fill="none" stroke="currentColor" stroke-width="2.2"/><path fill="currentColor" d="M13 10.5 22 16l-9 5.5v-11Z"/></svg>`,
  "tool:twitter": `<svg viewBox="0 0 24 24"><path fill="currentColor" d="M18.2 2h3.7l-8.1 9.3L23.3 22h-7.5l-5.9-7.7L3.2 22H-.5l8.7-9.9L-.9 2h7.7l5.3 7 6.1-7Zm-1.3 18.1h2L5.7 3.8H3.5l13.4 16.3Z"/></svg>`,
  "tool:zoho_mail": `<span class="integration-logo-word integration-logo-word-zoho">Zoho</span>`,
  custom_domain: `<svg viewBox="0 0 32 32"><circle cx="16" cy="16" r="11" fill="none" stroke="currentColor" stroke-width="2.2"/><path d="M5 16h22M16 5c3.3 3.1 5 6.8 5 11s-1.7 7.9-5 11M16 5c-3.3 3.1-5 6.8-5 11s1.7 7.9 5 11" fill="none" stroke="currentColor" stroke-width="2"/></svg>`,
};

// The bundled OpenAI image tool carries the same brand mark as the managed
// OpenAI network integration: one definition, two guide ids.
INTEGRATION_LOGOS["tool:openai_images"] = INTEGRATION_LOGOS.openai;

function integrationLogo(guide) {
  const mark = INTEGRATION_LOGOS[guide.id];
  const fallback = esc(String(guide.label || "?").trim().slice(0, 2).toUpperCase());
  const slug = guide.id.replace(/[^a-z0-9_-]/gi, "-");
  return `<span class="integration-logo integration-logo-${esc(slug)}" data-integration-logo="${esc(guide.id)}" data-logo-source="${mark ? "brand" : "fallback"}" aria-hidden="true">${mark || `<span class="integration-logo-word">${fallback}</span>`}</span>`;
}

function hideCallbackCopyFeedback() {
  if (copyFeedbackTimer) clearTimeout(copyFeedbackTimer);
  copyFeedbackTimer = null;
  for (const feedback of document.querySelectorAll("[data-callback-copy-feedback]")) {
    feedback.hidden = true;
  }
}

export function dismissCallbackCopyFeedback() {
  copyFeedbackGeneration += 1;
  hideCallbackCopyFeedback();
}

function legacyCopy(value) {
  const input = document.createElement("textarea");
  input.value = value;
  input.setAttribute("readonly", "");
  input.style.position = "fixed";
  input.style.opacity = "0";
  document.body.append(input);
  input.select();
  const copied = document.execCommand("copy");
  input.remove();
  if (!copied) throw new Error("Copy failed");
}

export async function copyCallbackUri(button) {
  const value = button.dataset.copyValue || "";
  const feedback = button.parentElement?.querySelector("[data-callback-copy-feedback]");
  const generation = ++copyFeedbackGeneration;
  hideCallbackCopyFeedback();
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(value);
    } else {
      legacyCopy(value);
    }
    if (generation !== copyFeedbackGeneration) return;
    if (feedback) {
      feedback.textContent = "Copied";
      feedback.hidden = false;
    }
    copyFeedbackTimer = setTimeout(dismissCallbackCopyFeedback, 2500);
  } catch (_error) {
    if (generation !== copyFeedbackGeneration) return;
    if (feedback) {
      feedback.textContent = "Copy failed";
      feedback.hidden = false;
      copyFeedbackTimer = setTimeout(dismissCallbackCopyFeedback, 2500);
    }
  }
}

function toolGuide(tool) {
  const oauth = tool.connection === "oauth";
  return {
    id: `tool:${tool.tool_id}`,
    label: tool.display_name,
    summary: tool.description,
    callbackUrl: oauth ? `${location.origin}/oauth/callback` : "",
    protections: Array.isArray(tool.protections) ? tool.protections : [],
    technicalDetails: Array.isArray(tool.technical_details) ? tool.technical_details : [],
    setupSteps: (tool.setup_steps || []).map(step => ({
      title: step.title,
      description: step.description,
      linkUrl: step.link_url,
      linkLabel: step.link_label,
      imagePath: step.image_path,
      imageAlt: step.image_alt,
      showCallback: step.show_callback,
      showConfig: step.show_config,
    })),
    capabilities: (tool.actions || []).map(action => ({
      name: action.id,
      codeName: true,
      description: action.description,
      approval: action.approval,
    })),
    dataSummary: {
      items: tool.data_summary.cards.map(card => ({
        title: card.title,
        description: card.description,
        points: card.points,
        links: card.links.map(link => ({ label: link.label, url: link.url })),
      })),
    },
    config: tool.config || [],
    networkScope: [],
  };
}

function allGuides(tools) {
  const managed = Object.entries(MANAGED_INTEGRATIONS).map(([id, guide]) => ({ id, ...guide }));
  const bundled = tools.map(toolGuide);
  return [...managed, ...bundled, CUSTOM_DOMAIN_GUIDE]
    .sort((left, right) => left.label.localeCompare(right.label, undefined, { sensitivity: "base" }));
}

function guideKind(guide) {
  return guide.id.startsWith("tool:")
    ? "Bundled MCP tool"
    : guide.id === "custom_domain"
      ? "Custom rule"
      : "Direct network integration";
}

function homeIntegrationCard(guide) {
  const tool = guide.id.startsWith("tool:");
  const status = tool
    ? `<span class="status ${guide.enabled ? "active" : "disabled"}" data-home-integration-status="${esc(guide.id)}">${guide.enabled ? "enabled" : "disabled"}</span>`
    : `<span class="status" data-home-integration-status="${esc(guide.id)}">loading</span>`;
  return `<button class="home-card home-integration-card" data-action="open-home-integration" data-guide="${esc(guide.id)}">
    <span class="home-integration-card-top"><span class="guide-kind">${esc(guideKind(guide))}</span>${status}</span>
    ${integrationLogo(guide)}
    <span class="home-card-copy"><strong>${esc(guide.label)}</strong><small>${esc(guide.summary)}</small></span>
    <span class="home-card-arrow">→</span>
  </button>`;
}

function sortHomeIntegrationCards() {
  for (const grid of document.querySelectorAll("#home-integration-groups .home-card-grid")) {
    const cards = Array.from(grid.querySelectorAll(".home-integration-card"));
    cards.sort((left, right) => {
      const leftEnabled = left.querySelector("[data-home-integration-status]")?.classList.contains("active") === true;
      const rightEnabled = right.querySelector("[data-home-integration-status]")?.classList.contains("active") === true;
      if (leftEnabled !== rightEnabled) return leftEnabled ? -1 : 1;
      const leftLabel = left.querySelector(".home-card-copy strong")?.textContent || "";
      const rightLabel = right.querySelector(".home-card-copy strong")?.textContent || "";
      return leftLabel.localeCompare(rightLabel, undefined, { sensitivity: "base" });
    });
    for (const card of cards) grid.append(card);
  }
}

document.addEventListener("kern-home-integration-statuses-updated", sortHomeIntegrationCards);

function renderHomeIntegrationGroups() {
  const byId = new Map(loadedGuides.map(guide => [guide.id, guide]));
  const inferenceIds = new Set(["openai", "claude", "bedrock"]);
  const groups = [
    ["AI inference", ["openai", "claude", "bedrock"].map(id => byId.get(id)).filter(Boolean)],
    ["Tools", loadedGuides.filter(guide => guide.id !== "custom_domain" && !inferenceIds.has(guide.id))],
    ["Manual", loadedGuides.filter(guide => guide.id === "custom_domain")],
  ];
  setHtml($("home-integration-groups"), groups.filter(([, guides]) => guides.length).map(([label, guides]) => `
    <div class="home-integration-group">
      <h3>${esc(label)}</h3>
      <div class="home-card-grid">${guides.map(homeIntegrationCard).join("")}</div>
    </div>`).join(""));
  sortHomeIntegrationCards();
  document.dispatchEvent(new CustomEvent("kern-home-integration-cards-rendered"));
}

export async function refreshConnectionGuide() {
  try {
    const response = await api("GET", "/v1/tools");
    const tools = Array.isArray(response.tools) ? response.tools : [];
    $("tools-cross-access-notice").hidden = tools.filter(tool => tool.enabled).length < 2;
    const toolState = new Map(tools.map(tool => [`tool:${tool.tool_id}`, tool.enabled === true]));
    loadedGuides = allGuides(tools).map(guide => ({ ...guide, enabled: toolState.get(guide.id) === true }));
    if (!loadedGuides.some(guide => guide.id === selectedGuideId)) {
      selectedGuideId = loadedGuides[0]?.id || "";
    }
    renderHomeIntegrationGroups();
    renderConnectionGuide();
  } catch (error) {
    const message = error && error.message ? error.message : String(error);
    setHtml($("connection-guide-content"), `<div class="empty-state">Could not load the bundled tools: ${esc(message)}</div>`);
    throw error;
  }
}

export function openConnectionGuide(guideId) {
  if (guideId) selectedGuideId = guideId;
  if (loadedGuides.some(guide => guide.id === selectedGuideId)) renderConnectionGuide();
}

function renderConnectionGuide() {
  const selected = loadedGuides.find(guide => guide.id === selectedGuideId) || loadedGuides[0];
  const content = $("connection-guide-content");
  setHtml(content, selected ? renderGuide(selected) : '<div class="empty-state">No integration guides are available.</div>');
  if (selected) {
    setHtml($("integration-detail-logo"), integrationLogo(selected));
    $("integration-detail-kind").textContent = guideKind(selected);
    $("integration-detail-title").textContent = selected.label;
    $("integration-detail-summary").textContent = selected.summary;
  }
}

function renderGuide(guide) {
  return `
    <article class="connection-guide-entry" data-guide-section="${esc(guide.id)}" tabindex="-1">
      <section class="guide-section">
        <h3>What it enables</h3>
        <div class="guide-capabilities">${guide.capabilities.map(renderCapability).join("")}</div>
      </section>
      <section class="guide-section">
        <h3>Connection</h3>
        ${renderSetup(guide)}
      </section>
      ${renderDataSummary(guide.dataSummary)}
      ${renderTechnicalDetails(guide)}
    </article>`;
}

function renderConfig(config) {
  if (!config || !config.length) return "";
  return `<div class="guide-config">
    <h4>Configuration values</h4>
    ${config.map(entry => `
      <div><code>${esc(entry.key)}</code><span>${esc(entry.description)}</span></div>
    `).join("")}
  </div>`;
}

function renderPolicyPoints(points) {
  if (!points || !points.length) return "";
  return `<div class="guide-policy-points">${points.map(point => `
    <div class="guide-policy-point"><span>${esc(point.label)}</span><p>${esc(point.text)}</p></div>`).join("")}
  </div>`;
}

function setupLinkIsInline(step) {
  return Boolean(step.linkUrl && step.linkLabel && String(step.description || "").includes(step.linkLabel));
}

function renderSetupDescription(step) {
  const description = String(step.description || "");
  if (!setupLinkIsInline(step)) return inlineCode(description);
  const index = description.indexOf(step.linkLabel);
  const before = description.slice(0, index);
  const after = description.slice(index + step.linkLabel.length);
  const link = `<a href="${esc(step.linkUrl)}" target="_blank" rel="noopener noreferrer">${esc(step.linkLabel)}</a>`;
  return `${inlineCode(before)}${link}${inlineCode(after)}`;
}

// The callback URI and configuration keys render inside the step that needs
// them, so the operator sees each value at the moment the provider asks for it.
function renderSetup(guide) {
  const steps = guide.setupSteps;
  if (!steps || !steps.length) return "";
  return `
    <ol class="guide-steps">${steps.map(step => `
      <li>
        <div class="guide-step-copy">
          <h4>${esc(step.title)}</h4>
          <p>${renderSetupDescription(step)}</p>
          ${step.code ? `<pre class="guide-step-code"><code>${esc(step.code)}</code></pre>` : ""}
          ${step.showCallback && guide.callbackUrl ? `<div class="guide-callback">
            <span class="guide-callback-label">Callback URI for this host</span>
            <div class="guide-callback-value">
              <code>${esc(guide.callbackUrl)}</code>
              <button class="guide-copy-button" data-action="copy-callback-uri" data-copy-value="${esc(guide.callbackUrl)}" aria-label="Copy callback URI" title="Copy callback URI">
                <svg viewBox="0 0 20 20" aria-hidden="true"><rect x="6.5" y="6.5" width="8" height="9" rx="1.5" fill="none" stroke="currentColor" stroke-width="1.5"/><path d="M5 13.5H4.5A1.5 1.5 0 0 1 3 12V4.5A1.5 1.5 0 0 1 4.5 3H11a1.5 1.5 0 0 1 1.5 1.5V5" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round"/></svg>
              </button>
              <span class="guide-copy-feedback" data-callback-copy-feedback role="status" hidden>Copied</span>
            </div>
          </div>` : ""}
          ${step.showConfig ? renderConfig(guide.config) : ""}
          ${step.linkUrl && !setupLinkIsInline(step) ? `<a href="${esc(step.linkUrl)}" target="_blank" rel="noopener noreferrer">${esc(step.linkLabel)}</a>` : ""}
        </div>
        ${step.imagePath ? `<figure><img src="${esc(step.imagePath)}" alt="${esc(step.imageAlt)}" loading="lazy"></figure>` : ""}
      </li>`).join("")}
    </ol>`;
}

function renderCapability(capability) {
  const approval = capability.approval === "operator"
    ? `<span class="status awaiting_login">approval required</span>`
    : capability.approval === "direct"
      ? `<span class="status active">runs directly</span>`
      : "";
  return `
    <div class="guide-capability">
      <div class="guide-capability-head"><h4>${capability.codeName ? `<code>${esc(capability.name)}</code>` : esc(capability.name)}</h4>${approval}</div>
      <p>${esc(capability.description)}</p>
      ${capability.linkUrl ? `<a href="${esc(capability.linkUrl)}" target="_blank" rel="noopener noreferrer">${esc(capability.linkLabel)}</a>` : ""}
    </div>`;
}

function renderDataSummary(summary) {
  return `
    <section class="guide-section guide-data-section">
      <h3>What happens to your data</h3>
      <div class="guide-data-summary">${summary.items.map(item => `
        <article>
          <h4>${esc(item.title)}</h4>
          ${item.description ? `<p>${esc(item.description)}</p>` : ""}
          ${renderPolicyPoints(item.points)}
          ${item.links.length ? `<div class="guide-data-summary-links">${item.links.map(link => `
            <a href="${esc(link.url)}" target="_blank" rel="noopener noreferrer">${esc(link.label)}</a>`).join("")}
          </div>` : ""}
        </article>`).join("")}
      </div>
    </section>`;
}

function renderTechnicalDetails(guide) {
  const notes = [...(guide.technicalDetails || []), ...(guide.controls || [])];
  const hasNetworkScope = Boolean(guide.networkScope && guide.networkScope.length);
  if (!notes.length && !hasNetworkScope) return "";
  return `
    <section class="guide-section guide-technical-details">
      <h3>Technical notes</h3>
      ${notes.length ? `<div class="guide-protections">
        <ul>${notes.map(item => `<li>${inlineCode(item)}</li>`).join("")}</ul>
      </div>` : ""}
      ${renderNetworkScope(guide.networkScope)}
    </section>`;
}

function renderNetworkScope(rows) {
  if (!rows || !rows.length) return "";
  return `
    <div class="guide-network-scope">
      <h4>Exact network boundary</h4>
      <div class="guide-network-rows">
        ${rows.map(([host, scope]) => `<div><code>${esc(host)}</code><span>${esc(scope)}</span></div>`).join("")}
      </div>
    </div>`;
}
