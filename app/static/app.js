const state = {
  mode: "auction",
};

const modeTitles = {
  auction: "Auction Levels",
  performance: "Month Map",
  regression: "Regression + EMAs",
  portfolio: "Portfolio",
  volatility: "Volatility",
  analysis: "Stock Brief",
};

const fieldRules = {
  auction: ["period"],
  performance: ["month"],
  regression: ["start-date", "end-date", "period"],
  portfolio: ["start-date", "end-date", "investment"],
  volatility: [],
  analysis: [],
};

const form = document.querySelector("#chart-form");
const outputTitle = document.querySelector("#output-title");
const imagesEl = document.querySelector("#images");
const errorEl = document.querySelector("#error");
const emptyState = document.querySelector("#empty-state");
const sourceChip = document.querySelector("#source-chip");
const summaryEl = document.querySelector("#summary");
const generateButton = document.querySelector("#generate");
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
    period: data.get("period"),
    month: Number(data.get("month")),
    start_date: data.get("start_date"),
    end_date: data.get("end_date"),
    investment_per_stock: Number(data.get("investment_per_stock") || 100),
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
  renderImages(data.images || []);
  renderSummary(data.meta || {});
}

async function fetchAnalysis(payload) {
  const response = await fetch(`/api/analysis/${payload.ticker}`);
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not fetch brief");
  }
  sourceChip.textContent = data.provider || "provider";
  renderAnalysis(data);
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
  const entries = Object.entries(flattenMeta(meta)).slice(0, 8);
  summaryEl.innerHTML = "";
  summaryEl.hidden = entries.length === 0;
  entries.forEach(([key, value]) => {
    summaryEl.append(summaryItem(labelize(key), formatValue(value)));
  });
}

function renderAnalysis(data) {
  imagesEl.innerHTML = "";
  emptyState.hidden = true;
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
  summaryEl.innerHTML = "";
  summaryEl.hidden = false;
  items.forEach(([label, value]) => summaryEl.append(summaryItem(label, value)));
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
    return Math.abs(value) >= 100 ? value.toFixed(0) : value.toFixed(2);
  }
  return String(value);
}

function clearOutput() {
  imagesEl.innerHTML = "";
  summaryEl.innerHTML = "";
  summaryEl.hidden = true;
  errorEl.hidden = true;
  errorEl.textContent = "";
  emptyState.hidden = false;
  sourceChip.textContent = "idle";
}

function showError(message) {
  emptyState.hidden = true;
  errorEl.textContent = message;
  errorEl.hidden = false;
}

function setLoading(isLoading) {
  generateButton.disabled = isLoading;
  generateButton.textContent = isLoading ? "Generating..." : "Generate";
}

boot();

