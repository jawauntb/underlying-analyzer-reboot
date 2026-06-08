const toolConfig = {
  vision: {
    title: "Vision",
    label: "Market Memo",
    icon: "/static/assets/vision.png",
    action: "See The Vision",
    endpoint: "/api/tools/vision",
    fields: ["ticker"],
    copy: "Generate a compact market memo from the rebuilt stock fax data.",
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
let lastExport = null;

boot();

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearOutput();
  setLoading(true);
  try {
    const data = await callTool();
    lastExport = data.export || data;
    exportButton.disabled = false;
    sourceEl.textContent = data.Provider || data.provider || data.meta?.ticker || activeTool.label;
    renderToolResult(data);
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
  setDefaultExpiry();
}

function setDefaultExpiry() {
  const input = document.querySelector("#tool-expiry");
  const date = new Date();
  const daysUntilFriday = (5 - date.getDay() + 7) % 7 || 7;
  date.setDate(date.getDate() + daysUntilFriday);
  input.value = date.toISOString().slice(0, 10);
}

async function callTool() {
  const formData = new FormData(form);
  const payload = {
    ticker: String(formData.get("ticker") || "AAPL").trim().toUpperCase(),
    expiry: formData.get("expiry"),
    prompt: formData.get("prompt"),
  };
  const response = await fetch(activeTool.endpoint, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || `${activeTool.title} failed`);
  }
  return data;
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
  renderSummary([
    ["Ticker", data.Ticker],
    ["Report", data["Text Provider"] || "Generated"],
  ]);
  const article = document.createElement("article");
  article.className = "memo-card";
  markdownBlocks(data["Market Memo"] || "").forEach((node) => article.append(node));
  resultEl.append(article);
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

function markdownBlocks(markdown) {
  return markdown
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .map((block) => {
      if (block.startsWith("### ")) {
        const heading = document.createElement("h3");
        heading.textContent = block.replace("### ", "");
        return heading;
      }
      const paragraph = document.createElement("p");
      paragraph.textContent = block.replaceAll("**", "");
      return paragraph;
    });
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
  submitButton.textContent = isLoading ? "Working..." : activeTool.action;
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
