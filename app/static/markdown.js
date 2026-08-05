/**
 * Minimal, dependency-free markdown renderer for streamed agent output.
 *
 * Everything is escaped before any markup is introduced, so model output can
 * never inject HTML. Supports the subset the agent actually emits: headings,
 * lists, tables, fenced and inline code, blockquotes, bold/italic, links, and
 * horizontal rules.
 */

const ESCAPES = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };

export function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, (char) => ESCAPES[char]);
}

export function renderMarkdown(source) {
  const text = String(source ?? "").replace(/\r\n/g, "\n");
  const blocks = [];
  const lines = text.split("\n");
  let index = 0;

  while (index < lines.length) {
    const line = lines[index];

    if (!line.trim()) {
      index += 1;
      continue;
    }

    if (line.trimStart().startsWith("```")) {
      const language = line.trim().slice(3).trim();
      const body = [];
      index += 1;
      while (index < lines.length && !lines[index].trimStart().startsWith("```")) {
        body.push(lines[index]);
        index += 1;
      }
      index += 1;
      blocks.push(
        `<pre class="md-pre"${language ? ` data-lang="${escapeHtml(language)}"` : ""}>` +
          `<code>${escapeHtml(body.join("\n"))}</code></pre>`,
      );
      continue;
    }

    const heading = line.match(/^(#{1,6})\s+(.*)$/);
    if (heading) {
      const level = Math.min(6, heading[1].length + 1);
      blocks.push(`<h${level}>${inline(heading[2])}</h${level}>`);
      index += 1;
      continue;
    }

    if (/^\s*([-*_])\1{2,}\s*$/.test(line)) {
      blocks.push('<hr class="md-rule" />');
      index += 1;
      continue;
    }

    if (isTableRow(line) && index + 1 < lines.length && isTableDivider(lines[index + 1])) {
      const header = splitRow(line);
      index += 2;
      const rows = [];
      while (index < lines.length && isTableRow(lines[index])) {
        rows.push(splitRow(lines[index]));
        index += 1;
      }
      blocks.push(renderTable(header, rows));
      continue;
    }

    if (/^\s*>\s?/.test(line)) {
      const quoted = [];
      while (index < lines.length && /^\s*>\s?/.test(lines[index])) {
        quoted.push(lines[index].replace(/^\s*>\s?/, ""));
        index += 1;
      }
      blocks.push(`<blockquote>${renderMarkdown(quoted.join("\n"))}</blockquote>`);
      continue;
    }

    const bulletMatch = line.match(/^\s*[-*+]\s+/);
    const orderedMatch = line.match(/^\s*\d+[.)]\s+/);
    if (bulletMatch || orderedMatch) {
      const ordered = Boolean(orderedMatch);
      const items = [];
      while (index < lines.length) {
        const current = lines[index];
        const marker = ordered
          ? current.match(/^\s*\d+[.)]\s+/)
          : current.match(/^\s*[-*+]\s+/);
        if (!marker) {
          if (current.trim() && items.length && /^\s{2,}\S/.test(current)) {
            items[items.length - 1] += ` ${current.trim()}`;
            index += 1;
            continue;
          }
          break;
        }
        items.push(current.slice(marker[0].length));
        index += 1;
      }
      const tag = ordered ? "ol" : "ul";
      const body = items.map((item) => `<li>${inline(item)}</li>`).join("");
      blocks.push(`<${tag} class="md-list">${body}</${tag}>`);
      continue;
    }

    const paragraph = [];
    while (index < lines.length && lines[index].trim() && !isBlockStart(lines[index])) {
      paragraph.push(lines[index].trim());
      index += 1;
    }
    if (paragraph.length) {
      blocks.push(`<p>${inline(paragraph.join(" "))}</p>`);
      continue;
    }
    index += 1;
  }

  return blocks.join("\n");
}

function isBlockStart(line) {
  return (
    /^\s*#{1,6}\s/.test(line) ||
    /^\s*[-*+]\s/.test(line) ||
    /^\s*\d+[.)]\s/.test(line) ||
    /^\s*>/.test(line) ||
    /^\s*```/.test(line) ||
    /^\s*([-*_])\1{2,}\s*$/.test(line) ||
    isTableRow(line)
  );
}

function isTableRow(line) {
  return /^\s*\|.*\|\s*$/.test(line);
}

function isTableDivider(line) {
  return /^\s*\|[\s:|-]+\|\s*$/.test(line) && line.includes("-");
}

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function renderTable(header, rows) {
  const head = header.map((cell) => `<th>${inline(cell)}</th>`).join("");
  const body = rows
    .map((row) => {
      const cells = header
        .map((_, position) => `<td>${inline(row[position] ?? "")}</td>`)
        .join("");
      return `<tr>${cells}</tr>`;
    })
    .join("");
  return `<div class="md-table-wrap"><table class="md-table"><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table></div>`;
}

function inline(source) {
  const codeSpans = [];
  let text = escapeHtml(source).replace(/`([^`]+)`/g, (_match, code) => {
    codeSpans.push(code);
    return `\u0000${codeSpans.length - 1}\u0000`;
  });

  text = text.replace(
    /\[([^\]]+)\]\((https?:\/\/[^\s)]+)\)/g,
    (_match, label, href) =>
      `<a href="${href}" target="_blank" rel="noopener noreferrer">${label}</a>`,
  );
  text = text.replace(
    /(^|[\s(])((?:https?:\/\/)[^\s<)]+)/g,
    (_match, lead, href) =>
      `${lead}<a href="${href}" target="_blank" rel="noopener noreferrer">${href}</a>`,
  );
  text = text.replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>");
  text = text.replace(/(^|[^*])\*([^*\n]+)\*/g, "$1<em>$2</em>");
  text = text.replace(/(^|\s)_([^_\n]+)_(?=\s|$|[.,!?])/g, "$1<em>$2</em>");
  text = text.replace(/~~([^~]+)~~/g, "<del>$1</del>");

  return text.replace(
    /\u0000(\d+)\u0000/g,
    (_match, position) => `<code>${codeSpans[Number(position)]}</code>`,
  );
}
