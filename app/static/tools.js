const toolConfig = {
  vision: {
    title: "Vision",
    label: "Market Memo",
    icon: "/static/assets/vision.png",
    action: "See The Vision",
    endpoint: "/api/tools/vision",
    fields: ["ticker"],
    copy: "Generate a professional analyst memo from the rebuilt stock fax data.",
  },
  pixel: {
    title: "Pixel",
    label: "Image Generator",
    icon: "/static/assets/toro.png",
    action: "Generate Image",
    endpoint: "/api/tools/pixel",
    fields: ["prompt"],
    copy: "Create an 8-bit market image when OPENAI_API_KEY is configured.",
  },
  fax: {
    title: "Stock Fax",
    label: "Stock Analysis",
    icon: "/static/assets/fax.png",
    action: "Get Stock Fax",
    endpoint: "/api/tools/fax",
    fields: ["ticker"],
    copy: "Fetch volatility, trend, EMA, auction levels, and snapshot metrics.",
  },
  moneyline: {
    title: "Moneyline",
    label: "Options Map",
    icon: "/static/assets/moneyline.png",
    action: "View Moneyline",
    endpoint: "/api/tools/moneyline",
    fields: ["ticker", "expiry"],
    copy: "Render a call/put open-interest map around the current price.",
  },
};

const pathKey = location.pathname.replace("/", "") || "vision";
const activeTool = toolConfig[pathKey] || toolConfig.vision;
const form = document.querySelector("#tool-form");
const submitButton = document.querySelector("#tool-submit");
const exportButton = document.querySelector("#tool-export");
const outputTitle = document.querySelector("#tool-output-title");
const resultEl = document.querySelector("#tool-result");
const summaryEl = document.querySelector("#tool-summary");
const errorEl = document.querySelector("#tool-error");
const emptyEl = document.querySelector("#tool-empty");
const sourceEl = document.querySelector("#tool-source");
const pdfButton = createPdfButton();
let lastExport = null;

boot();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearOutput();
  setLoading(true);
  try {
    if (pathKey === "vision") {
      await streamVisionTool();
    } else {
      const data = await callTool();
      renderCompletedResult(data);
    }
  } catch (error) {
    showError(error.message || "Tool failed");
  } finally {
    setLoading(false);
  }
});

exportButton.addEventListener("click", () => {
  if (!lastExport) {
    return;
  }
  const filename = `${pathKey}-${new Date().toISOString().replace(/[:.]/g, "-")}.json`;
  downloadJson(lastExport, filename);
});

pdfButton.addEventListener("click", () => {
  if (lastExport) {
    window.print();
  }
});

function boot() {
  document.title = `${activeTool.title} by The Underlying`;
  document.querySelector("#legacy-title").textContent = activeTool.title;
  document.querySelector("#legacy-heading").textContent = activeTool.title;
  document.querySelector("#tool-label").textContent = activeTool.label;
  document.querySelector("#legacy-copy").textContent = activeTool.copy;
  document.querySelector("#legacy-icon").src = activeTool.icon;
  submitButton.textContent = activeTool.action;
  document.querySelector(`[data-tool-link="${pathKey}"]`)?.classList.add("active");
  document.querySelector("#ticker-field").hidden = !activeTool.fields.includes("ticker");
  document.querySelector("#expiry-field").hidden = !activeTool.fields.includes("expiry");
  document.querySelector("#prompt-field").hidden = !activeTool.fields.includes("prompt");
  document.querySelector("#tool-empty img").src = activeTool.icon;
  document.querySelector("#tool-empty > span").textContent = activeTool.label;
  setDefaultExpiry();
}

function createPdfButton() {
  const button = document.createElement("button");
  button.className = "export-button pdf-button";
  button.id = "tool-export-pdf";
  button.type = "button";
  button.textContent = "Export PDF";
  button.disabled = true;
  exportButton.after(button);
  return button;
}

function setDefaultExpiry() {
  const input = document.querySelector("#tool-expiry");
  const date = new Date();
  const daysUntilFriday = (5 - date.getDay() + 7) % 7 || 7;
  date.setDate(date.getDate() + daysUntilFriday);
  input.value = date.toISOString().slice(0, 10);
}

async function callTool() {
  const response = await fetch(activeTool.endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toolPayload()),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `${activeTool.title} failed`);
  }
  return data;
}

function toolPayload() {
  const formData = new FormData(form);
  return {
    ticker: String(formData.get("ticker") || "AAPL").trim().toUpperCase(),
    expiry: formData.get("expiry"),
    prompt: formData.get("prompt"),
  };
}

async function streamVisionTool() {
  const response = await fetch("/api/tools/vision/stream", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(toolPayload()),
  });
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || "Vision failed");
  }
  if (!response.body) {
    renderCompletedResult(await callTool());
    return;
  }

  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  const state = {
    buffer: "",
    data: null,
    frame: null,
    memo: "",
  };

  while (true) {
    const { done, value } = await reader.read();
    if (done) {
      break;
    }
    state.buffer += decoder.decode(value, { stream: true });
    processVisionStreamLines(state, false);
  }
  state.buffer += decoder.decode();
  processVisionStreamLines(state, true);
}

function processVisionStreamLines(state, flush) {
  const lines = state.buffer.split("\n");
  state.buffer = flush ? "" : lines.pop();
  lines.forEach((line) => {
    if (!line.trim()) {
      return;
    }
    handleVisionStreamEvent(state, JSON.parse(line));
  });
}

function handleVisionStreamEvent(state, event) {
  if (event.type === "meta") {
    state.data = event;
    outputTitle.textContent = activeTool.title;
    sourceEl.textContent = "streaming";
    state.frame = renderVisionFrame(event, { streaming: true });
    updateMemoBody(state.frame.body, "", true);
    emptyEl.hidden = true;
    return;
  }

  if (event.type === "token") {
    state.memo += event.text || "";
    if (state.frame) {
      updateMemoBody(state.frame.body, state.memo, true);
      state.frame.status.textContent = "Streaming";
    }
    return;
  }

  if (event.type === "done") {
    state.memo = event.text || state.memo;
    const data = {
      ...(state.data || {}),
      ...event,
      "Market Memo": state.memo,
    };
    if (state.frame) {
      updateMemoBody(state.frame.body, state.memo, false);
      state.frame.status.textContent = "Complete";
      state.frame.status.classList.add("complete");
    }
    lastExport = event.export || data;
    exportButton.disabled = false;
    pdfButton.disabled = false;
    sourceEl.textContent = data["Text Model"] || data["Text Provider"] || activeTool.label;
    renderSummary(visionSummaryItems(data));
    return;
  }

  if (event.type === "error") {
    throw new Error(event.error || "Vision stream failed");
  }
}

function renderCompletedResult(data) {
  lastExport = data.export || data;
  exportButton.disabled = false;
  pdfButton.disabled = false;
  sourceEl.textContent = data.Provider || data.provider || data.meta?.ticker || activeTool.label;
  renderToolResult(data);
}

function renderToolResult(data) {
  emptyEl.hidden = true;
  outputTitle.textContent = activeTool.title;
  if (pathKey === "vision") {
    renderVision(data);
  } else if (pathKey === "fax") {
    renderFax(data);
  } else if (pathKey === "moneyline") {
    renderMoneyline(data);
  } else {
    renderPixel(data);
  }
}

function renderVision(data) {
  const frame = renderVisionFrame(data);
  updateMemoBody(frame.body, data["Market Memo"] || "", false);
}

function renderVisionFrame(data, options = {}) {
  renderSummary(visionSummaryItems(data));
  resultEl.innerHTML = "";

  const article = document.createElement("article");
  article.className = "memo-card vision-memo";

  const header = document.createElement("div");
  header.className = "memo-header";

  const titleGroup = document.createElement("div");
  const eyebrow = document.createElement("span");
  eyebrow.className = "memo-eyebrow";
  eyebrow.textContent = "Market Memo";
  const heading = document.createElement("h3");
  heading.textContent = `${data.Ticker || data.meta?.ticker || "Vision"} Vision`;
  titleGroup.append(eyebrow, heading);

  const meta = document.createElement("div");
  meta.className = "memo-meta";
  meta.append(
    memoChip(data["Text Provider"] || "anthropic"),
    memoChip(data["Text Model"] || "model"),
    memoChip(data.provider || data.Report?.Provider || "market data"),
  );
  const status = memoChip(options.streaming ? "Streaming" : "Complete");
  status.classList.add("memo-status");
  meta.append(status);

  const body = document.createElement("div");
  body.className = "memo-body";

  header.append(titleGroup, meta);
  article.append(header, body);
  resultEl.append(article);
  return { article, body, status };
}

function visionSummaryItems(data) {
  const report = data.Report || {};
  return [
    ["Ticker", data.Ticker || report.Ticker],
    ["Price", report.Snapshot?.Price],
    ["Setup", report["Signal Summary"]?.Setup],
    ["Model", data["Text Model"] || data["Text Provider"] || "Generated"],
  ];
}

function memoChip(value) {
  const chip = document.createElement("span");
  chip.className = "memo-chip";
  chip.textContent = formatValue(value);
  return chip;
}

function updateMemoBody(body, markdown, streaming) {
  body.innerHTML = "";
  if (markdown.trim()) {
    renderMarkdown(markdown, body);
  } else {
    const standby = document.createElement("p");
    standby.className = "memo-standby";
    standby.textContent = "Opening tape...";
    body.append(standby);
  }
  if (streaming) {
    const cursor = document.createElement("span");
    cursor.className = "stream-cursor";
    body.append(cursor);
  }
}

function renderFax(data) {
  renderSummary([
    ["Ticker", data.Ticker],
    ["Price", data.Snapshot?.Price],
    ["Setup", data["Signal Summary"]?.Setup],
    ["Provider", data["Text Provider"] || data.Provider],
  ]);
  const stack = document.createElement("div");
  stack.className = "report-stack";
  [
    ["Anthropic Report", data["Anthropic Report"]],
    ["Snapshot", data.Snapshot],
    ["Volatility Metrics", data["Volatility Metrics"]],
    ["Regression Trend", data["Regression Trend"]],
    ["EMAs Summary", data["EMAs Summary"]],
    ["Auction Market Theory Price Levels", data["Auction Market Theory Price Levels"]],
    ["Signal Summary", data["Signal Summary"]],
  ].forEach(([title, value]) => stack.append(reportSection(title, value)));
  resultEl.append(stack);
}

function renderMoneyline(data) {
  renderSummary([
    ["Ticker", data.meta?.ticker],
    ["Expiry", data.meta?.expiry],
    ["Spot", data.meta?.current_price],
    ["Rows", data.meta?.rows?.length],
  ]);
  const image = document.createElement("img");
  image.className = "tool-image";
  image.src = `data:${data.image.mime};base64,${data.image.data}`;
  image.alt = data.image.filename || "Moneyline chart";
  resultEl.append(image);
  resultEl.append(downloadLink(image.src, data.image.filename || "moneyline.png"));
}

function renderPixel(data) {
  renderSummary([
    ["Prompt", data.prompt],
    ["Created", data.created],
  ]);
  const image = document.createElement("img");
  image.className = "pixel-image";
  image.src = `data:${data.image.mime};base64,${data.image.data}`;
  image.alt = "Generated Pixel";
  resultEl.append(image);
  resultEl.append(downloadLink(image.src, data.image.filename || "pixel.png"));
}

function renderSummary(items) {
  summaryEl.innerHTML = "";
  items.forEach(([label, value]) => summaryEl.append(summaryItem(label, formatValue(value))));
  summaryEl.hidden = false;
}

function summaryItem(label, value) {
  const item = document.createElement("div");
  item.className = "summary-item";
  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  const valueEl = document.createElement("strong");
  valueEl.textContent = value;
  item.append(labelEl, valueEl);
  return item;
}

function reportSection(title, value) {
  const section = document.createElement("section");
  section.className = "report-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  if (Array.isArray(value)) {
    value.forEach((item) => section.append(reportRow("", item)));
  } else if (value && typeof value === "object") {
    Object.entries(value).forEach(([key, child]) => section.append(reportRow(key, child)));
  } else {
    const paragraph = document.createElement("p");
    paragraph.textContent = formatValue(value);
    section.append(paragraph);
  }
  return section;
}

function reportRow(label, value) {
  const row = document.createElement("div");
  row.className = "report-row";
  const key = document.createElement("span");
  key.textContent = label;
  const val = document.createElement("strong");
  val.textContent = formatValue(value);
  row.append(key, val);
  return row;
}

function renderMarkdown(markdown, container) {
  markdownBlocks(markdown).forEach((node) => container.append(node));
}

function markdownBlocks(markdown) {
  return markdown
    .replace(/\r\n/g, "\n")
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .flatMap((block) => markdownBlock(block));
}

function markdownBlock(block) {
  const lines = block.split("\n").map((line) => line.trim()).filter(Boolean);
  if (lines.length >= 2 && isMarkdownTable(lines)) {
    return [markdownTable(lines)];
  }
  if (lines.every((line) => /^[-*]\s+/.test(line))) {
    return [markdownList(lines)];
  }
  if (block.startsWith("### ")) {
    const heading = document.createElement("h3");
    heading.textContent = block.replace(/^###\s+/, "");
    return [heading];
  }
  if (block.startsWith("## ")) {
    const heading = document.createElement("h3");
    heading.textContent = block.replace(/^##\s+/, "");
    return [heading];
  }

  const paragraph = document.createElement("p");
  lines.forEach((line, index) => {
    if (index > 0) {
      paragraph.append(document.createElement("br"));
    }
    appendInlineMarkdown(paragraph, line);
  });
  return [paragraph];
}

function isMarkdownTable(lines) {
  return lines.some((line) => /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(line));
}

function markdownTable(lines) {
  const separatorIndex = lines.findIndex((line) =>
    /^\|?\s*:?-{3,}:?\s*(\|\s*:?-{3,}:?\s*)+\|?$/.test(line),
  );
  const headerCells = parseTableRow(lines[Math.max(0, separatorIndex - 1)]);
  const bodyLines = lines.slice(separatorIndex + 1);
  const wrapper = document.createElement("div");
  wrapper.className = "memo-table-wrap";
  const table = document.createElement("table");
  table.className = "memo-table";
  const thead = document.createElement("thead");
  const headerRow = document.createElement("tr");
  headerCells.forEach((cell) => {
    const th = document.createElement("th");
    appendInlineMarkdown(th, cell);
    headerRow.append(th);
  });
  thead.append(headerRow);
  const tbody = document.createElement("tbody");
  bodyLines.forEach((line) => {
    const row = document.createElement("tr");
    parseTableRow(line).forEach((cell) => {
      const td = document.createElement("td");
      appendInlineMarkdown(td, cell);
      row.append(td);
    });
    tbody.append(row);
  });
  table.append(thead, tbody);
  wrapper.append(table);
  return wrapper;
}

function parseTableRow(line) {
  return line
    .replace(/^\|/, "")
    .replace(/\|$/, "")
    .split("|")
    .map((cell) => cell.trim());
}

function markdownList(lines) {
  const list = document.createElement("ul");
  lines.forEach((line) => {
    const item = document.createElement("li");
    appendInlineMarkdown(item, line.replace(/^[-*]\s+/, ""));
    list.append(item);
  });
  return list;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /\*\*(.+?)\*\*/g;
  let cursor = 0;
  let match = pattern.exec(text);
  while (match) {
    if (match.index > cursor) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    const strong = document.createElement("strong");
    strong.textContent = match[1];
    parent.append(strong);
    cursor = match.index + match[0].length;
    match = pattern.exec(text);
  }
  if (cursor < text.length) {
    parent.append(document.createTextNode(text.slice(cursor)));
  }
}

function downloadLink(href, filename) {
  const link = document.createElement("a");
  link.className = "download-link";
  link.href = href;
  link.download = filename;
  link.textContent = "Download";
  return link;
}

function formatValue(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
  }
  if (Array.isArray(value)) {
    return value.length ? JSON.stringify(value) : "None";
  }
  if (value && typeof value === "object") {
    return JSON.stringify(value);
  }
  if (value === 0) {
    return "0";
  }
  return value ? String(value) : "N/A";
}

function clearOutput() {
  resultEl.innerHTML = "";
  summaryEl.innerHTML = "";
  summaryEl.hidden = true;
  errorEl.hidden = true;
  errorEl.textContent = "";
  sourceEl.textContent = "idle";
  emptyEl.hidden = false;
  lastExport = null;
  exportButton.disabled = true;
  pdfButton.disabled = true;
}

function showError(message) {
  emptyEl.hidden = true;
  errorEl.textContent = message;
  errorEl.hidden = false;
}

function setLoading(isLoading) {
  document.body.classList.toggle("is-loading", isLoading);
  submitButton.disabled = isLoading;
  exportButton.disabled = isLoading || !lastExport;
  pdfButton.disabled = isLoading || !lastExport;
  submitButton.textContent = isLoading
    ? pathKey === "vision"
      ? "Streaming..."
      : "Working..."
    : activeTool.action;
}

function downloadJson(payload, filename) {
  const blob = new Blob([JSON.stringify(payload, null, 2)], { type: "application/json" });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.append(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}
