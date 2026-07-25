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
      const key = `${String(event.task_id || "")}\u0000${String(activityId)}`;
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
      compacted[existingIndex] = {
        ...event,
        payload: {
          ...event.payload,
          activity: {
            ...previous,
            ...current,
            title: genericUpdate && previous.title ? previous.title : current.title,
            kind: genericUpdate && previous.kind ? previous.kind : current.kind,
            ...(output === undefined ? {} : { output }),
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
    text = text.replace(/(!?)\[([^\]\n]+)\]\(([^)\s]+)(?:\s+"[^"]*")?\)/g, (_, image, label, rawHref) => {
      const href = safeHref(rawHref);
      if (!href) return image ? `[image: ${label}]` : label;
      const safe = escapeHtml(href);
      const textLabel = escapeHtml(label);
      const prefix = image ? "Image: " : "";
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
    renderMarkdown,
    safeHref,
    clipUtf8,
    compactActivityEvents,
  };
  root.KernRichText = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis === "undefined" ? this : globalThis);
