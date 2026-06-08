const state = {
  mode: "auction",
  lastExport: null,
};

const modeTitles = {
  auction: "Auction Levels",
  performance: "Month Map",
  regression: "Regression + EMAs",
  portfolio: "Portfolio",
  volatility: "Volatility",
  analysis: "Stock Brief",
};

const sharedFields = ["watchlist-url", "max-results"];

const fieldRules = {
  auction: [...sharedFields, "period"],
  performance: [...sharedFields, "month"],
  regression: [...sharedFields, "start-date", "end-date", "period"],
  portfolio: [...sharedFields, "start-date", "end-date", "investment", "benchmark"],
  volatility: [...sharedFields],
  analysis: [...sharedFields],
};

const form = document.querySelector("#chart-form");
const outputTitle = document.querySelector("#output-title");
const imagesEl = document.querySelector("#images");
const errorEl = document.querySelector("#error");
const warningsEl = document.querySelector("#warnings");
const emptyState = document.querySelector("#empty-state");
const sourceChip = document.querySelector("#source-chip");
const summaryEl = document.querySelector("#summary");
const generateButton = document.querySelector("#generate");
const exportButton = document.querySelector("#export-json");
const providerLabel = document.querySelector("#provider-label");
const healthDot = document.querySelector("#health-dot");

document.querySelectorAll(".mode-button").forEach((button) => {
  button.addEventListener("click", () => {
    state.mode = button.dataset.mode;
    document.querySelectorAll(".mode-button").forEach((item) => item.classList.remove("active"));
    button.classList.add("active");
    outputTitle.textContent = modeTitles[state.mode];
    syncFields();
    clearOutput();
  });
});

exportButton.addEventListener("click", () => {
  if (!state.lastExport) {
    return;
  }
  downloadJson(state.lastExport, exportFilename());
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearOutput();
  setLoading(true);

  try {
    const payload = payloadFromForm();
    if (state.mode === "analysis") {
      await fetchAnalysis(payload);
    } else {
      await fetchChart(payload);
    }
  } catch (error) {
    showError(error.message || "Request failed");
  } finally {
    setLoading(false);
  }
});

async function boot() {
  setDefaultDates();
  syncFields();
  try {
    const health = await fetch("/api/health").then((response) => response.json());
    const providers = await fetch("/api/providers").then((response) => response.json());
    if (health.ok) {
      healthDot.classList.add("ok");
      providerLabel.textContent = `${providers.primary} + ${providers.fallback}`;
    }
  } catch {
    providerLabel.textContent = "provider offline";
  }
}

function setDefaultDates() {
  const now = new Date();
  const start = new Date(now);
  start.setFullYear(now.getFullYear() - 1);
  document.querySelector("#start-date").value = start.toISOString().slice(0, 10);
  document.querySelector("#end-date").value = now.toISOString().slice(0, 10);
  document.querySelector("#month").value = String(now.getMonth() + 1);
}

function syncFields() {
  const visible = new Set(fieldRules[state.mode] || []);
  document.querySelectorAll("[data-field]").forEach((field) => {
    field.hidden = !visible.has(field.dataset.field);
  });
}

function payloadFromForm() {
  const data = new FormData(form);
  const tickers = String(data.get("tickers") || "AAPL")
    .split(",")
    .map((ticker) => ticker.trim().toUpperCase())
    .filter(Boolean);

  return {
    ticker: tickers[0],
    tickers,
    watchlist_url: data.get("watchlist_url"),
    max_results: Number(data.get("max_results") || 10),
    period: data.get("period"),
    month: Number(data.get("month")),
    start_date: data.get("start_date"),
    end_date: data.get("end_date"),
    investment_per_stock: Number(data.get("investment_per_stock") || 100),
    benchmark_ticker: data.get("benchmark_ticker"),
  };
}

async function fetchChart(payload) {
  const response = await fetch(`/api/charts/${state.mode}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not generate chart");
  }
  sourceChip.textContent = data.provider || "provider";
  state.lastExport = data.export || data;
  exportButton.disabled = false;
  renderImages(data.images || []);
  renderSummary(data.meta || {});
  renderWarnings(data.meta?.errors || []);
}

async function fetchAnalysis(payload) {
  const response = await fetch("/api/analysis", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not fetch brief");
  }
  sourceChip.textContent = data.provider || "provider";
  state.lastExport = data.export || data;
  exportButton.disabled = false;
  renderAnalysis(data);
  renderWarnings(data.meta?.errors || []);
}

function renderImages(images) {
  imagesEl.innerHTML = "";
  emptyState.hidden = images.length > 0;
  images.forEach((image, index) => {
    const card = document.createElement("div");
    card.className = "chart-card";

    const img = document.createElement("img");
    img.src = `data:${image.mime};base64,${image.data}`;
    img.alt = image.filename || `Generated chart ${index + 1}`;

    const link = document.createElement("a");
    link.className = "download-link";
    link.href = img.src;
    link.download = image.filename || `chart-${index + 1}.png`;
    link.textContent = "Download";

    card.append(img, link);
    imagesEl.append(card);
  });
}

function renderSummary(meta) {
  const flat = flattenMeta(meta);
  const preferred = [
    "result_count",
    "error_count",
    "watchlist_name",
    "portfolio_final",
    "total_return",
    "benchmark_ticker",
    "benchmark_return",
    "alpha_vs_benchmark",
    "max_drawdown",
    "annualized_volatility",
    "scanner_count",
  ];
  const entries = preferred
    .filter((key) => Object.hasOwn(flat, key))
    .map((key) => [key, flat[key]]);
  Object.entries(flat).forEach(([key, value]) => {
    if (entries.length < 8 && !preferred.includes(key)) {
      entries.push([key, value]);
    }
  });
  summaryEl.innerHTML = "";
  summaryEl.hidden = entries.length === 0;
  entries.slice(0, 8).forEach(([key, value]) => {
    summaryEl.append(summaryItem(labelize(key), formatMetaValue(key, value)));
  });
}

function renderAnalysis(data) {
  imagesEl.innerHTML = "";
  emptyState.hidden = true;
  const summaries = data.summaries || [data];
  summaryEl.innerHTML = "";
  summaryEl.hidden = false;
  summaryEl.append(summaryItem("Results", summaries.length));
  if (data.meta?.error_count) {
    summaryEl.append(summaryItem("Skipped", data.meta.error_count));
  }
  if (data.meta?.watchlist_name) {
    summaryEl.append(summaryItem("Source", data.meta.watchlist_name));
  }
  if (data.scanner?.length) {
    summaryEl.append(summaryItem("Scanner Rows", data.scanner.length));
  }

  const stack = document.createElement("div");
  stack.className = "brief-stack";
  if (data.scanner?.length > 1) {
    stack.append(scannerTable(data.scanner));
  }
  summaries.forEach((summary) => stack.append(briefCard(summary)));
  imagesEl.append(stack);
}

function scannerTable(rows) {
  const panel = document.createElement("section");
  panel.className = "scanner-panel";
  const title = document.createElement("h3");
  title.textContent = "Watchlist Scanner";
  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "scanner-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Rank</th>
        <th>Ticker</th>
        <th>Price</th>
        <th>Day</th>
        <th>50D</th>
        <th>52W High</th>
        <th>Vol</th>
        <th>Score</th>
      </tr>
    </thead>
  `;
  const body = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    [
      row.rank,
      row.ticker,
      formatValue(row.price),
      formatPercent(row.change_percent / 100),
      formatPercent(row.trend_50d),
      formatPercent(row.distance_from_52w_high),
      formatPercent(row.annual_volatility),
      formatValue(row.score),
    ].forEach((value) => {
      const td = document.createElement("td");
      td.textContent = value;
      tr.append(td);
    });
    body.append(tr);
  });
  table.append(body);
  tableWrap.append(table);
  panel.append(title, tableWrap);
  return panel;
}

function briefCard(data) {
  const card = document.createElement("article");
  card.className = "brief-card";
  const title = document.createElement("h3");
  title.textContent = data.ticker;
  const items = [
    ["Name", data.name],
    ["Price", data.price],
    ["Change", `${formatValue(data.change)} (${formatValue(data.change_percent)}%)`],
    ["Market Cap", data.market_cap],
    ["Sector", data.sector],
    ["Annual Vol", `${formatValue(data.annual_volatility * 100)}%`],
    ["50D Trend", `${formatValue(data.trend_50d * 100)}%`],
    ["52W Range", `${formatValue(data.fifty_two_week_low)} - ${formatValue(data.fifty_two_week_high)}`],
  ];
  const grid = document.createElement("div");
  grid.className = "brief-grid";
  items.forEach(([label, value]) => grid.append(summaryItem(label, value)));
  card.append(title, grid);
  return card;
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

function flattenMeta(meta) {
  const flat = {};
  Object.entries(meta).forEach(([key, value]) => {
    if (value && typeof value === "object" && !Array.isArray(value)) {
      Object.entries(value).forEach(([childKey, childValue]) => {
        flat[`${key}_${childKey}`] = childValue;
      });
    } else if (Array.isArray(value)) {
      flat[key] = `${value.length} items`;
    } else {
      flat[key] = value;
    }
  });
  return flat;
}

function labelize(key) {
  return key.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function formatValue(value) {
  if (typeof value === "number" && Number.isFinite(value)) {
    if (Number.isInteger(value)) {
      return String(value);
    }
    return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
  }
  if (value && typeof value === "object") {
    return JSON.stringify(value);
  }
  return String(value);
}

function formatMetaValue(key, value) {
  if (
    [
      "total_return",
      "benchmark_return",
      "alpha_vs_benchmark",
      "max_drawdown",
      "annualized_volatility",
    ].includes(key)
  ) {
    return formatPercent(Number(value));
  }
  return formatValue(value);
}

function formatPercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "N/A";
  }
  return `${formatValue(number * 100)}%`;
}

function renderWarnings(errors) {
  warningsEl.innerHTML = "";
  warningsEl.hidden = !errors.length;
  errors.forEach((item) => {
    const warning = document.createElement("div");
    warning.className = "warning-item";
    warning.textContent = `${item.ticker || "Skipped"}: ${item.error || "No data"}`;
    warningsEl.append(warning);
  });
}

function clearOutput() {
  imagesEl.innerHTML = "";
  summaryEl.innerHTML = "";
  summaryEl.hidden = true;
  errorEl.hidden = true;
  errorEl.textContent = "";
  warningsEl.innerHTML = "";
  warningsEl.hidden = true;
  emptyState.hidden = false;
  sourceChip.textContent = "idle";
  state.lastExport = null;
  exportButton.disabled = true;
}

function showError(message) {
  emptyState.hidden = true;
  errorEl.textContent = message;
  errorEl.hidden = false;
}

function setLoading(isLoading) {
  generateButton.disabled = isLoading;
  exportButton.disabled = isLoading || !state.lastExport;
  generateButton.textContent = isLoading ? "Generating..." : "Generate";
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

function exportFilename() {
  const stamp = new Date().toISOString().replace(/[:.]/g, "-");
  return `${state.mode}-${stamp}.json`;
}

boot();
