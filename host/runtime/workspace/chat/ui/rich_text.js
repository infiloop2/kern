(function (root) {
  "use strict";

  function escapeHtml(value) {
    return String(value ?? "")
      .replaceAll("&", "&amp;")
      .replaceAll("<", "&lt;")
      .replaceAll(">", "&gt;")
      .replaceAll('"', "&quot;")
      .replaceAll("'", "&#39;");
  }

  function safeHref(value) {
    const href = String(value ?? "").trim();
    if (/^(https?:|mailto:)/i.test(href)) return href;
    return "";
  }

  const AGENT_HOME_PATH = "/mnt/kern-agent/agent-home";

  function workspaceFilePath(value) {
    const path = String(value ?? "").trim();
    if (!path || path.length > 4096 || path.includes("\0")) return "";
    if (path !== AGENT_HOME_PATH && !path.startsWith(`${AGENT_HOME_PATH}/`)) return "";
    const relative = path.slice(AGENT_HOME_PATH.length);
    if (relative.split("/").includes("..")) return "";
    return relative || "/";
  }

  function workspaceFileFallbackPath(value) {
    const path = String(value ?? "").trim();
    const stripped = path.replace(/:\d+(?::\d+)?$/, "");
    if (stripped === path) return "";
    return workspaceFilePath(stripped);
  }

  // This is deliberately a browser-navigation allowlist, not a reflection of
  // Kern's agent network policy. Keep it to exact, human-facing provider
  // hosts: API, OAuth, CDN, redirector, and arbitrary search-result hosts do
  // not belong here.
  const SAFE_NAVIGATION_HOSTS = new Set([
    "api-dashboard.search.brave.com",
    "calendar.google.com",
    "console.byteplus.com",
    "console.cloud.google.com",
    "dev.runwayml.com",
    "developer.x.com",
    "developers.google.com",
    "docs.byteplus.com",
    "docs.dev.runwayml.com",
    "docs.github.com",
    "docs.polymarket.com",
    "docs.x.com",
    "github.com",
    "help.runwayml.com",
    "instagram.com",
    "linkedin.com",
    "mail.google.com",
    "policies.google.com",
    "polymarket.com",
    "privacycenter.instagram.com",
    "runwayml.com",
    "search.brave.com",
    "twitter.com",
    "www.google.com",
    "www.instagram.com",
    "www.interactivebrokers.com",
    "www.linkedin.com",
    "x.com",
  ]);

  function parsedSafeNavigationUrl(value) {
    const href = String(value ?? "").trim();
    if (!href || href.length > 8192) return null;
    try {
      const url = new URL(href);
      if (
        url.protocol !== "https:" || url.port || url.username || url.password
        || !SAFE_NAVIGATION_HOSTS.has(url.hostname)
      ) return null;
      return url;
    } catch (_error) {
      return null;
    }
  }

  function safeNavigationHref(value) {
    const url = parsedSafeNavigationUrl(value);
    if (!url) return "";
    let path;
    try {
      path = decodeURIComponent(url.pathname).toLowerCase();
    } catch (_error) {
      return "";
    }
    if (url.hostname === "x.com" || url.hostname === "twitter.com") {
      if (path.startsWith("/i/oauth2/")) return "";
    }
    if (
      ["instagram.com", "www.instagram.com"].includes(url.hostname)
      && path.startsWith("/oauth/")
    ) return "";
    if (
      ["linkedin.com", "www.linkedin.com"].includes(url.hostname)
      && path.startsWith("/oauth/")
    ) return "";
    if (url.hostname === "github.com" && path.startsWith("/login/oauth/")) return "";
    if (url.hostname === "www.google.com") {
      if (path !== "/calendar/event" || url.hash) return "";
      const allowed = new Set(["ctz", "eid"]);
      for (const key of url.searchParams.keys()) if (!allowed.has(key)) return "";
      const eventIds = url.searchParams.getAll("eid");
      const timeZones = url.searchParams.getAll("ctz");
      if (eventIds.length !== 1 || !eventIds[0] || eventIds[0].length > 2048) return "";
      if (timeZones.length > 1 || (timeZones[0] || "").length > 100) return "";
    }
    return url.href;
  }

  const TRUNCATION_SUFFIX = "\n… (truncated)";
  const ACTIVITY_HISTORY_OUTPUT_BYTES = 2 * 1024 * 1024;
  const MAX_BLOCKQUOTE_DEPTH = 16;

  function clipUtf8(value, maximum = ACTIVITY_HISTORY_OUTPUT_BYTES) {
    const text = String(value ?? "");
    const encoder = new TextEncoder();
    const decoder = new TextDecoder();
    const encoded = encoder.encode(text);
    if (encoded.length <= maximum) return text;
    const suffix = encoder.encode(TRUNCATION_SUFFIX);
    const suffixText = maximum >= suffix.length ? TRUNCATION_SUFFIX : "";
    const prefixBytes = Math.max(maximum - (suffixText ? suffix.length : 0), 0);
    let prefix = decoder.decode(encoded.slice(0, prefixBytes));
    while (prefix && encoder.encode(prefix + suffixText).length > maximum) {
      prefix = prefix.slice(0, -1);
    }
    return prefix + suffixText;
  }

  function compactActivityEvents(events) {
    const compacted = [];
    const activityIndexes = new Map();
    for (const event of events) {
      const current = event && event.payload && event.payload.activity;
      const activityId = current && current.activity_id;
      if (!current || typeof current !== "object" || !activityId) {
        compacted.push(event);
        continue;
      }
      // The host scopes provider ids to their private execution before
      // returning them, so one flat thread stream can safely merge snapshots
      // by activity_id without public lifecycle boundaries.
      const key = String(activityId);
      const existingIndex = activityIndexes.get(key);
      if (existingIndex === undefined) {
        const output = Object.prototype.hasOwnProperty.call(current, "output")
          ? clipUtf8(current.output)
          : undefined;
        compacted.push({
          ...event,
          payload: {
            ...event.payload,
            activity: {
              ...current,
              ...(output === undefined ? {} : { output }),
            },
          },
        });
        activityIndexes.set(key, compacted.length - 1);
        continue;
      }
      const previousEvent = compacted[existingIndex];
      const previous = previousEvent.payload.activity;
      const genericUpdate = ["Tool result", "Tool progress", "Command output"].includes(current.title);
      let output = previous.output;
      if (current.append_output) {
        output = clipUtf8(`${previous.output || ""}${current.output || ""}`);
      } else if (Object.prototype.hasOwnProperty.call(current, "output")) {
        output = clipUtf8(current.output);
      }
      // A streamed detail carries only its own chunk, so the accumulated text
      // lives here rather than in every stored event. Without this the live
      // card would show the newest fragment alone, usually mid-sentence.
      let detail = previous.detail;
      if (current.append_detail) {
        detail = clipUtf8(`${previous.detail || ""}${current.detail || ""}`);
      } else if (Object.prototype.hasOwnProperty.call(current, "detail")) {
        detail = clipUtf8(current.detail);
      }
      compacted[existingIndex] = {
        ...event,
        // The snapshot is rendered where the activity first appeared. Keep
        // that stable ordering key when later updates replace its contents so
        // a subsequent merge/sort cannot move it across intervening messages.
        seq: previousEvent.seq,
        payload: {
          ...event.payload,
          activity: {
            ...previous,
            ...current,
            title: genericUpdate && previous.title ? previous.title : current.title,
            kind: genericUpdate && previous.kind ? previous.kind : current.kind,
            ...(output === undefined ? {} : { output }),
            ...(detail === undefined ? {} : { detail }),
          },
        },
      };
    }
    return compacted;
  }

  function inlineMarkdown(source) {
    const tokens = [];
    const stash = html => {
      const index = tokens.push(html) - 1;
      return `\u0000KERN${index}\u0000`;
    };
    let text = String(source ?? "");
    text = text.replace(/`([^`\n]+)`/g, (_, code) =>
      stash(`<code>${escapeHtml(code)}</code>`));
    text = text.replace(/(!?)\[([^\]\n]+)\]\((?:<([^>\n]+)>|([^)\s]+))(?:\s+"[^"]*")?\)/g,
      (_, image, label, bracketedHref, plainHref) => {
      const rawHref = bracketedHref || plainHref;
      const filePath = !image && workspaceFilePath(rawHref);
      if (filePath) {
        const fallbackPath = workspaceFileFallbackPath(rawHref);
        const fallbackAttribute = fallbackPath
          ? ` data-fallback-path="${escapeHtml(fallbackPath)}"`
          : "";
        return stash(
          `<button type="button" class="md-open-file" ` +
          `data-file-path="${escapeHtml(filePath)}"${fallbackAttribute} ` +
          `title="Open in Agent workspace">${escapeHtml(label)}</button>`,
        );
      }
      const href = safeHref(rawHref);
      if (!href) return image ? `[image: ${label}]` : label;
      const safe = escapeHtml(href);
      const textLabel = escapeHtml(label);
      const prefix = image ? "Image: " : "";
      const navigationHref = !image && safeNavigationHref(href);
      if (navigationHref) {
        return stash(
          `<a class="md-open-link" href="${escapeHtml(navigationHref)}" ` +
          `title="${escapeHtml(navigationHref)}" target="_blank" ` +
          `rel="noopener noreferrer">${textLabel}</a>`,
        );
      }
      return stash(
        `<button type="button" class="md-copy-link" data-copy-href="${safe}" ` +
        `title="Copy link">${prefix}${textLabel}</button>`,
      );
    });
    text = escapeHtml(text)
      .replace(/\*\*([^*\n]+)\*\*/g, "<strong>$1</strong>")
      .replace(/__([^_\n]+)__/g, "<strong>$1</strong>")
      .replace(/~~([^~\n]+)~~/g, "<del>$1</del>")
      .replace(/(^|[\s(])\*([^*\n]+)\*(?=$|[\s).,!?:;])/g, "$1<em>$2</em>")
      .replace(/(^|[\s(])_([^_\n]+)_(?=$|[\s).,!?:;])/g, "$1<em>$2</em>");
    return text.replace(/\u0000KERN(\d+)\u0000/g, (_, index) => tokens[Number(index)] || "");
  }

  function isTableDivider(line) {
    return /^\s*\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?\s*$/.test(line);
  }

  function tableCells(line) {
    return line.trim().replace(/^\|/, "").replace(/\|$/, "").split("|").map(cell => cell.trim());
  }

  function renderMarkdown(source, blockquoteDepth = 0) {
    const lines = String(source ?? "").replace(/\r\n?/g, "\n").split("\n");
    const html = [];
    let index = 0;
    const startsBlock = line =>
      !line.trim() ||
      /^(```|~~~)/.test(line.trim()) ||
      /^#{1,6}\s+/.test(line) ||
      /^\s*>/.test(line) ||
      /^\s*([-+*]|\d+\.)\s+/.test(line) ||
      /^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line);

    while (index < lines.length) {
      const line = lines[index];
      if (!line.trim()) {
        index += 1;
        continue;
      }
      const fence = line.trim().match(/^(```|~~~)\s*([\w.+-]*)\s*$/);
      if (fence) {
        const marker = fence[1];
        const language = fence[2];
        const code = [];
        index += 1;
        while (index < lines.length && !lines[index].trim().startsWith(marker)) {
          code.push(lines[index]);
          index += 1;
        }
        if (index < lines.length) index += 1;
        const label = language ? escapeHtml(language) : "code";
        html.push(
          `<div class="md-code"><div class="md-code-head"><span>${label}</span>` +
          `<button type="button" class="md-copy">Copy</button></div>` +
          `<pre><code>${escapeHtml(code.join("\n"))}</code></pre></div>`,
        );
        continue;
      }
      const heading = line.match(/^(#{1,6})\s+(.+)$/);
      if (heading) {
        const level = heading[1].length;
        html.push(`<h${level}>${inlineMarkdown(heading[2])}</h${level}>`);
        index += 1;
        continue;
      }
      if (/^\s*([-*_])(?:\s*\1){2,}\s*$/.test(line)) {
        html.push("<hr>");
        index += 1;
        continue;
      }
      if (index + 1 < lines.length && line.includes("|") && isTableDivider(lines[index + 1])) {
        const headings = tableCells(line);
        index += 2;
        const rows = [];
        while (index < lines.length && lines[index].includes("|") && lines[index].trim()) {
          rows.push(tableCells(lines[index]));
          index += 1;
        }
        html.push(
          `<div class="md-table-wrap"><table><thead><tr>${headings.map(cell =>
            `<th>${inlineMarkdown(cell)}</th>`).join("")}</tr></thead><tbody>${rows.map(row =>
            `<tr>${headings.map((_, cellIndex) =>
              `<td>${inlineMarkdown(row[cellIndex] || "")}</td>`).join("")}</tr>`).join("")}</tbody></table></div>`,
        );
        continue;
      }
      if (/^\s*>/.test(line)) {
        const quoted = [];
        while (index < lines.length && /^\s*>/.test(lines[index])) {
          quoted.push(lines[index].replace(/^\s*>\s?/, ""));
          index += 1;
        }
        const content = blockquoteDepth < MAX_BLOCKQUOTE_DEPTH
          ? renderMarkdown(quoted.join("\n"), blockquoteDepth + 1)
          : `<p>${inlineMarkdown(quoted.join("\n"))}</p>`;
        html.push(`<blockquote>${content}</blockquote>`);
        continue;
      }
      const list = line.match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
      if (list) {
        const ordered = /\d+\./.test(list[1]);
        const tag = ordered ? "ol" : "ul";
        const items = [];
        while (index < lines.length) {
          const match = lines[index].match(/^\s*([-+*]|\d+\.)\s+(.+)$/);
          if (!match || /\d+\./.test(match[1]) !== ordered) break;
          let item = match[2];
          const task = item.match(/^\[([ xX])\]\s+(.+)$/);
          if (task) {
            item = `<input type="checkbox" disabled${task[1].toLowerCase() === "x" ? " checked" : ""}> ${inlineMarkdown(task[2])}`;
          } else {
            item = inlineMarkdown(item);
          }
          items.push(`<li>${item}</li>`);
          index += 1;
        }
        html.push(`<${tag}>${items.join("")}</${tag}>`);
        continue;
      }
      const paragraph = [line.trim()];
      index += 1;
      while (index < lines.length && !startsBlock(lines[index])) {
        if (index + 1 < lines.length && lines[index].includes("|") && isTableDivider(lines[index + 1])) break;
        paragraph.push(lines[index].trim());
        index += 1;
      }
      html.push(`<p>${paragraph.map(inlineMarkdown).join("<br>")}</p>`);
    }
    return html.join("");
  }

  const api = {
    escapeHtml,
    safeNavigationHref,
    workspaceFilePath,
    workspaceFileFallbackPath,
    renderMarkdown,
    safeHref,
    clipUtf8,
    compactActivityEvents,
  };
  root.KernRichText = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis === "undefined" ? this : globalThis);
