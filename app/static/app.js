import {
  mountAlertMonitor,
  mountAccountControls,
  mountResearchLibrary,
  mountSavedWatchlistCockpit,
} from "./research.js";

const state = {
  mode: "auction",
  lastExport: null,
  viewerPreviousFocus: null,
};

const modeTitles = {
  auction: "Auction Levels",
  performance: "Month Map",
  regression: "Regression + EMAs",
  "ridge-growth": "Ridge Growth",
  "flow-compass": "Flow Compass",
  cockpit: "Watchlist Cockpit",
  alerts: "Alert Digest",
  portfolio: "Portfolio",
  volatility: "Volatility",
  analysis: "Stock Brief",
};

const modeContracts = {
  auction:
    "Maps value, POC, and range acceptance; watch VAH/VAL breaks to judge where price is accepting or rejecting.",
  performance:
    "Compares historical month behavior; use the current period against typical drift, hit rate, and range.",
  regression:
    "Shows trend channel and EMA structure; use slope, band position, and average reclaim/loss to judge trend health.",
  "ridge-growth":
    "Runs the Ridge daily trend strategy over 6M, 1Y, and 2Y windows with Flow Compass and auction-market context.",
  "flow-compass":
    "Scores main bias from signed volume, trend, momentum, value location, and relative volatility direction.",
  cockpit:
    "Ranks a watchlist by scanner strength, Ridge state, Flow Compass, auction location, and risk so the first names to inspect are obvious.",
  alerts:
    "Turns the cockpit run into a prioritized digest of setups, risk flags, flow shifts, auction breaks, and volatility alerts.",
  portfolio:
    "Builds a watchlist portfolio run; compare return, drawdown, volatility, and benchmark alpha before sizing ideas.",
  volatility:
    "Ranks realized volatility and expected range; use it to size risk and spot regime changes across the list.",
  analysis:
    "Generates an equity brief and scanner pass; use ranks, sector context, and data gaps as diligence starters.",
};

const sharedFields = ["watchlist-url", "max-results"];

const fieldRules = {
  auction: [...sharedFields, "period"],
  performance: [...sharedFields, "month"],
  regression: [...sharedFields, "start-date", "end-date", "period"],
  "ridge-growth": [...sharedFields],
  "flow-compass": [...sharedFields, "period"],
  cockpit: [...sharedFields, "period"],
  alerts: [...sharedFields, "period", "max-alerts", "vol-threshold"],
  portfolio: [...sharedFields, "start-date", "end-date", "investment", "benchmark"],
  volatility: [...sharedFields],
  analysis: [...sharedFields],
};

const form = document.querySelector("#chart-form");
const outputTitle = document.querySelector("#output-title");
const modeContractEl = document.querySelector("#mode-contract");
const outputContractEl = document.querySelector("#output-contract");
const imagesEl = document.querySelector("#images");
const errorEl = document.querySelector("#error");
const warningsEl = document.querySelector("#warnings");
const emptyState = document.querySelector("#empty-state");
const sourceChip = document.querySelector("#source-chip");
const summaryEl = document.querySelector("#summary");
const outputPanel = document.querySelector(".output-panel");
const generateButton = document.querySelector("#generate");
const exportButton = document.querySelector("#export-json");
const providerLabel = document.querySelector("#provider-label");
const healthDot = document.querySelector("#health-dot");
const formActionsEl = document.querySelector("#chart-form .form-actions");
const mobileLayoutQuery = window.matchMedia("(max-width: 680px)");
const chartViewer = createChartViewer();
mountAccountControls({ root: document.querySelector("#account-control") });
mountSavedWatchlistCockpit({
  root: document.querySelector("#saved-watchlists"),
  getDraft: watchlistDraft,
  applyWatchlist: applySavedWatchlist,
  runCockpit: runSavedCockpit,
  runAlerts: runSavedAlertRule,
});
const researchLibrary = mountResearchLibrary({
  insertAfter: formActionsEl,
  getRecord: buildResearchRecord,
  getTicker: () => payloadFromForm().ticker,
  openRecord: openSavedResearch,
});
const alertMonitor = mountAlertMonitor({
  insertAfter: researchLibrary.root || formActionsEl,
  getDraft: alertRuleDraft,
  openRun: openSavedAlertRun,
  runRule: runSavedAlertRule,
});
syncSecondaryPanelPlacement();
mobileLayoutQuery.addEventListener("change", syncSecondaryPanelPlacement);

document.querySelectorAll(".mode-button").forEach((button) => {
  button.addEventListener("click", () => {
    setMode(button.dataset.mode);
  });
});

exportButton.addEventListener("click", () => {
  if (!state.lastExport) {
    return;
  }
  downloadJson(state.lastExport, exportFilename());
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !chartViewer.root.hidden) {
    closeChartViewer();
  }
});

form.addEventListener("submit", async (event) => {
  event.preventDefault();
  clearOutput();
  setLoading(true);

  try {
    const payload = payloadFromForm();
    if (state.mode === "analysis") {
      await fetchAnalysis(payload);
    } else if (state.mode === "cockpit") {
      await fetchCockpit(payload);
    } else if (state.mode === "alerts") {
      await fetchAlerts(payload);
    } else {
      await fetchChart(payload);
    }
    focusOutputOnMobile();
  } catch (error) {
    showError(error.message || "Request failed");
    focusOutputOnMobile();
  } finally {
    setLoading(false);
  }
});

async function boot() {
  setDefaultDates();
  syncModeCopy();
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

function syncModeCopy() {
  const contract = modeContracts[state.mode];
  outputTitle.textContent = modeTitles[state.mode];
  modeContractEl.textContent = contract;
  outputContractEl.textContent = contract;
}

function setMode(mode, options = {}) {
  const nextMode = modeTitles[mode] ? mode : "auction";
  state.mode = nextMode;
  document.querySelectorAll(".mode-button").forEach((button) => {
    button.classList.toggle("active", button.dataset.mode === state.mode);
  });
  syncModeCopy();
  syncFields();
  if (options.clear !== false) {
    clearOutput();
  }
}

function syncSecondaryPanelPlacement() {
  if (!formActionsEl || !outputPanel) {
    return;
  }
  const panels = [researchLibrary.root, alertMonitor.root].filter(Boolean);
  let anchor = mobileLayoutQuery.matches ? outputPanel : formActionsEl;
  panels.forEach((panel) => {
    anchor.after(panel);
    anchor = panel;
  });
}

function focusOutputOnMobile() {
  if (!mobileLayoutQuery.matches) {
    return;
  }
  const behavior = window.matchMedia("(prefers-reduced-motion: reduce)").matches ? "auto" : "smooth";
  outputPanel?.scrollIntoView({ block: "start", behavior });
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
    max_alerts: Number(data.get("max_alerts") || 12),
    volatility_threshold: Number(data.get("volatility_threshold") || 0.55),
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
  researchLibrary.setCanSave(true);
  renderImages(data.images || []);
  renderChartExtras(data);
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
  researchLibrary.setCanSave(true);
  renderAnalysis(data);
  renderWarnings(data.meta?.errors || []);
}

async function fetchCockpit(payload) {
  const response = await fetch("/api/watchlists/cockpit", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not build cockpit");
  }
  sourceChip.textContent = data.provider || "provider";
  state.lastExport = data.export || data;
  exportButton.disabled = false;
  researchLibrary.setCanSave(true);
  renderCockpit(data);
  renderWarnings(data.meta?.errors || []);
}

async function fetchAlerts(payload) {
  const response = await fetch("/api/watchlists/alerts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not build alerts");
  }
  sourceChip.textContent = data.provider || "provider";
  state.lastExport = data.export || data;
  exportButton.disabled = false;
  researchLibrary.setCanSave(true);
  renderAlerts(data);
  renderWarnings(data.meta?.errors || []);
}

function renderImages(images) {
  imagesEl.innerHTML = "";
  emptyState.hidden = images.length > 0;
  images.forEach((image, index) => {
    const src = `data:${image.mime};base64,${image.data}`;
    const filename = image.filename || `chart-${index + 1}.png`;

    const card = document.createElement("div");
    card.className = "chart-card";

    const previewButton = document.createElement("button");
    previewButton.className = "chart-preview-button";
    previewButton.type = "button";
    previewButton.setAttribute("aria-label", `Inspect ${filename}`);

    const img = document.createElement("img");
    img.src = src;
    img.alt = filename;

    previewButton.append(img);
    previewButton.addEventListener("click", () => openChartViewer({ src, filename }));

    const actions = document.createElement("div");
    actions.className = "chart-actions";
    const inspectButton = chartActionButton("Inspect chart", () =>
      openChartViewer({ src, filename }),
    );
    inspectButton.classList.add("chart-action-primary");
    const openLink = chartActionLink("Open PNG", src, filename, false);
    openLink.classList.add("chart-action-secondary");
    const downloadLink = chartActionLink("Download", src, filename, true);
    downloadLink.classList.add("chart-action-secondary");
    actions.append(inspectButton, openLink, downloadLink);

    card.append(previewButton, actions);
    imagesEl.append(card);
  });
}

function renderChartExtras(data) {
  const meta = data.meta || {};
  if (!meta.analysis_memo && !meta.windows?.length) {
    return;
  }
  const stack = document.createElement("div");
  stack.className = "brief-stack";
  if (meta.analysis_memo) {
    stack.append(markdownPanel("Ridge + Flow Memo", meta.analysis_memo));
  }
  if (meta.windows?.length) {
    stack.append(ridgeWindowTable(meta.windows));
  }
  imagesEl.append(stack);
  emptyState.hidden = true;
}

function ridgeWindowTable(windows) {
  const panel = document.createElement("section");
  panel.className = "scanner-panel";
  const title = document.createElement("h3");
  title.textContent = "Ridge Windows";
  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "scanner-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Window</th>
        <th>State</th>
        <th>Read</th>
        <th>Return</th>
        <th>Drawdown</th>
        <th>Flow</th>
        <th>Auction</th>
      </tr>
    </thead>
  `;
  const body = document.createElement("tbody");
  windows.forEach((window) => {
    const tr = document.createElement("tr");
    [
      String(window.period || "").toUpperCase(),
      window.state,
      window.recommendation,
      formatPercent(window.total_return),
      formatPercent(window.max_drawdown),
      `${window.flow_compass?.state || "N/A"} ${formatValue(
        window.flow_compass?.score ?? "N/A",
      )}`,
      window.auction?.location || "N/A",
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

function chartActionButton(label, onClick) {
  const button = document.createElement("button");
  button.className = "download-link chart-action-button";
  button.type = "button";
  button.textContent = label;
  button.addEventListener("click", onClick);
  return button;
}

function chartActionLink(label, href, filename, download) {
  const link = document.createElement("a");
  link.className = "download-link";
  link.href = href;
  link.textContent = label;
  if (download) {
    link.download = filename;
  } else {
    link.target = "_blank";
    link.rel = "noopener noreferrer";
  }
  return link;
}

function createChartViewer() {
  const root = document.createElement("div");
  root.className = "chart-viewer";
  root.hidden = true;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-labelledby", "chart-viewer-title");

  const panel = document.createElement("div");
  panel.className = "chart-viewer-panel";

  const head = document.createElement("div");
  head.className = "chart-viewer-head";

  const titleGroup = document.createElement("div");
  const label = document.createElement("div");
  label.className = "panel-label";
  label.textContent = "Chart";
  const title = document.createElement("h2");
  title.id = "chart-viewer-title";
  title.textContent = "Chart";
  titleGroup.append(label, title);

  const actions = document.createElement("div");
  actions.className = "chart-viewer-actions";
  const openLink = document.createElement("a");
  openLink.className = "download-link";
  openLink.textContent = "Open PNG";
  openLink.target = "_blank";
  openLink.rel = "noopener noreferrer";
  const downloadLink = document.createElement("a");
  downloadLink.className = "download-link";
  downloadLink.textContent = "Download";
  const closeButton = document.createElement("button");
  closeButton.className = "download-link chart-viewer-close";
  closeButton.type = "button";
  closeButton.textContent = "Close";
  closeButton.addEventListener("click", closeChartViewer);
  actions.append(openLink, downloadLink, closeButton);

  const imageWrap = document.createElement("div");
  imageWrap.className = "chart-viewer-image-wrap";
  const image = document.createElement("img");
  image.className = "chart-viewer-image";
  image.alt = "Expanded chart";
  imageWrap.append(image);

  panel.append(head, imageWrap);
  head.append(titleGroup, actions);
  root.append(panel);
  root.addEventListener("click", (event) => {
    if (event.target === root) {
      closeChartViewer();
    }
  });
  document.body.append(root);

  return { root, title, image, openLink, downloadLink, closeButton };
}

function openChartViewer({ src, filename }) {
  state.viewerPreviousFocus = document.activeElement;
  chartViewer.title.textContent = filename;
  chartViewer.image.src = src;
  chartViewer.image.alt = filename;
  chartViewer.openLink.href = src;
  chartViewer.downloadLink.href = src;
  chartViewer.downloadLink.download = filename;
  chartViewer.root.hidden = false;
  document.body.classList.add("chart-viewer-open");
  chartViewer.closeButton.focus();
}

function closeChartViewer() {
  chartViewer.root.hidden = true;
  document.body.classList.remove("chart-viewer-open");
  if (state.viewerPreviousFocus?.focus) {
    state.viewerPreviousFocus.focus();
  }
  state.viewerPreviousFocus = null;
}

function renderSummary(meta) {
  const flat = flattenMeta(meta);
  const preferred = [
    "result_count",
    "error_count",
    "watchlist_name",
    "portfolio_final",
    "ending_equity",
    "total_return",
    "state",
    "recommendation",
    "flow_state",
    "flow_score",
    "auction_location",
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
  if (data["Anthropic Brief"]) {
    stack.append(markdownPanel("Anthropic Brief", data["Anthropic Brief"]));
  }
  if (data.scanner?.length > 1) {
    stack.append(scannerTable(data.scanner));
  }
  summaries.forEach((summary) => stack.append(briefCard(summary)));
  imagesEl.append(stack);
}

function renderCockpit(data) {
  imagesEl.innerHTML = "";
  emptyState.hidden = true;
  const rows = data.rows || data.export?.rows || [];
  summaryEl.innerHTML = "";
  summaryEl.hidden = false;
  summaryEl.append(summaryItem("Rows", rows.length));
  if (data.meta?.error_count) {
    summaryEl.append(summaryItem("Skipped", data.meta.error_count));
  }
  if (data.meta?.watchlist_name) {
    summaryEl.append(summaryItem("Source", data.meta.watchlist_name));
  }
  if (data.meta?.period) {
    summaryEl.append(summaryItem("Window", String(data.meta.period).toUpperCase()));
  }

  const stack = document.createElement("div");
  stack.className = "brief-stack";
  stack.append(cockpitTable(rows));
  imagesEl.append(stack);
}

function renderAlerts(data) {
  imagesEl.innerHTML = "";
  emptyState.hidden = true;
  const alerts = data.alerts || data.export?.alerts || [];
  const digest = data.digest || data.export?.digest || {};
  summaryEl.innerHTML = "";
  summaryEl.hidden = false;
  summaryEl.append(summaryItem("Alerts", alerts.length));
  summaryEl.append(summaryItem("High", data.meta?.high_alert_count || 0));
  if (data.meta?.medium_alert_count) {
    summaryEl.append(summaryItem("Medium", data.meta.medium_alert_count));
  }
  if (data.meta?.watchlist_name) {
    summaryEl.append(summaryItem("Source", data.meta.watchlist_name));
  }
  if (data.meta?.period) {
    summaryEl.append(summaryItem("Window", String(data.meta.period).toUpperCase()));
  }

  const stack = document.createElement("div");
  stack.className = "brief-stack";
  stack.append(alertDigestPanel(digest), alertQueue(alerts));
  imagesEl.append(stack);
}

function alertDigestPanel(digest) {
  const panel = document.createElement("section");
  panel.className = "scanner-panel alert-digest-panel";
  const title = document.createElement("h3");
  title.textContent = "Alert Digest";
  const headline = document.createElement("p");
  headline.className = "alert-headline";
  headline.textContent = digest.headline || "No alerts returned.";
  const summary = document.createElement("p");
  summary.className = "alert-digest-copy";
  summary.textContent = digest.summary || "";
  const grid = document.createElement("div");
  grid.className = "alert-digest-grid";
  const severity = digest.severity_counts || {};
  const lanes = digest.lane_counts || {};
  grid.append(
    summaryItem("High", severity.High || 0),
    summaryItem("Medium", severity.Medium || 0),
    summaryItem("Priority", lanes.Priority || 0),
    summaryItem("Risk", lanes.Risk || 0),
  );
  panel.append(title, headline, summary, grid);
  if (digest.next_steps?.length) {
    const list = document.createElement("ul");
    list.className = "alert-next-steps";
    digest.next_steps.forEach((step) => {
      const item = document.createElement("li");
      item.textContent = step;
      list.append(item);
    });
    panel.append(list);
  }
  return panel;
}

function alertQueue(alerts) {
  const panel = document.createElement("section");
  panel.className = "scanner-panel alert-panel";
  const title = document.createElement("h3");
  title.textContent = "Alert Queue";
  panel.append(title);
  if (!alerts.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = "No alerts fired for this run.";
    panel.append(empty);
    return panel;
  }
  const list = document.createElement("div");
  list.className = "alert-list";
  alerts.forEach((alert) => list.append(alertCard(alert)));
  panel.append(list);
  return panel;
}

function alertCard(alert) {
  const card = document.createElement("article");
  card.className = `alert-card alert-${String(alert.severity || "info").toLowerCase()}`;
  const head = document.createElement("div");
  head.className = "alert-card-head";
  const badge = document.createElement("span");
  badge.className = "alert-severity";
  badge.textContent = alert.severity || "Info";
  const title = document.createElement("strong");
  title.textContent = `${alert.ticker || "N/A"} - ${alert.title || "Alert"}`;
  head.append(badge, title);

  const message = document.createElement("p");
  message.textContent = alert.message || "";
  const action = document.createElement("p");
  action.className = "alert-action";
  action.textContent = alert.action || "";

  const meta = document.createElement("div");
  meta.className = "alert-meta";
  [
    alert.category,
    alert.lane,
    `Score ${formatValue(alert.score)}`,
    `Rank ${formatValue(alert.rank)}`,
  ]
    .filter(Boolean)
    .forEach((value) => {
      const chip = document.createElement("span");
      chip.textContent = value;
      meta.append(chip);
    });

  card.append(head, message, action, meta);
  return card;
}

function cockpitTable(rows) {
  const panel = document.createElement("section");
  panel.className = "scanner-panel cockpit-panel";
  const title = document.createElement("h3");
  title.textContent = "Watchlist Cockpit";
  const tableWrap = document.createElement("div");
  tableWrap.className = "table-wrap";
  const table = document.createElement("table");
  table.className = "scanner-table cockpit-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Rank</th>
        <th>Ticker</th>
        <th>Lane</th>
        <th>Score</th>
        <th>Price</th>
        <th>Day</th>
        <th>50D</th>
        <th>Flow</th>
        <th>Ridge</th>
        <th>Auction</th>
      </tr>
    </thead>
  `;
  const body = document.createElement("tbody");
  rows.forEach((row) => {
    const tr = document.createElement("tr");
    const values = [
      row.rank,
      row.ticker,
      row.lane,
      formatValue(row.score),
      formatCurrency(row.price),
      formatPercent(row.change_percent / 100),
      formatPercent(row.trend_50d),
      `${row.flow?.state || "N/A"} (${formatValue(row.flow?.score)})`,
      row.ridge?.recommendation || "N/A",
      row.auction?.location || "N/A",
    ];
    values.forEach((value, index) => {
      const cell = document.createElement(index === 1 ? "th" : "td");
      if (index === 1) {
        cell.scope = "row";
      }
      cell.textContent = value;
      tr.append(cell);
    });
    body.append(tr);
  });
  table.append(body);
  tableWrap.append(table);
  panel.append(title, tableWrap);

  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = "No cockpit rows returned.";
    panel.append(empty);
  }
  return panel;
}

function markdownPanel(title, markdown) {
  const panel = document.createElement("section");
  panel.className = "scanner-panel";
  const heading = document.createElement("h3");
  heading.textContent = title;
  panel.append(heading);
  String(markdown || "")
    .split(/\n{2,}/)
    .map((block) => block.trim())
    .filter(Boolean)
    .forEach((block) => {
      const paragraph = document.createElement("p");
      paragraph.textContent = block.replaceAll("**", "").replace(/^#+\s*/, "");
      panel.append(paragraph);
    });
  return panel;
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
  if (["portfolio_final", "ending_equity"].includes(key)) {
    return formatCurrency(Number(value));
  }
  return formatValue(value);
}

function formatCurrency(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "N/A";
  }
  return `$${number.toLocaleString(undefined, { maximumFractionDigits: 0 })}`;
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
  closeChartViewer();
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
  researchLibrary.setCanSave(false);
}

function watchlistDraft() {
  const payload = payloadFromForm();
  return {
    source_url: String(payload.watchlist_url || "").trim(),
    tickers: payload.tickers || [],
    max_results: payload.max_results,
  };
}

function alertRuleDraft() {
  const payload = payloadFromForm();
  return {
    source_url: String(payload.watchlist_url || "").trim(),
    tickers: payload.tickers || [],
    max_results: payload.max_results,
    max_alerts: payload.max_alerts,
    volatility_threshold: payload.volatility_threshold,
    period: payload.period,
  };
}

function applySavedWatchlist(row) {
  const watchlistUrl = document.querySelector("#watchlist-url");
  const tickers = document.querySelector("#tickers");
  const maxResults = document.querySelector("#max-results");
  watchlistUrl.value = row.source_url || "";
  tickers.value = Array.isArray(row.tickers) ? row.tickers.join(", ") : "";
  if (row.metadata?.max_results) {
    maxResults.value = row.metadata.max_results;
  }
  setMode("cockpit");
}

async function runSavedCockpit(row) {
  const payload = watchlistActionPayload(row);
  clearOutput();
  setMode("cockpit", { clear: false });
  setLoading(true);
  try {
    await fetchCockpit(payload);
  } catch (error) {
    showError(error.message || "Could not run saved cockpit");
    throw error;
  } finally {
    setLoading(false);
  }
}

async function runSavedAlertRule(row) {
  const payload = alertRulePayload(row);
  clearOutput();
  setMode("alerts", { clear: false });
  setLoading(true);
  try {
    const response = await fetch("/api/watchlists/alerts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Could not run alert rule");
    }
    sourceChip.textContent = data.provider || "provider";
    state.lastExport = data.export || data;
    exportButton.disabled = false;
    researchLibrary.setCanSave(true);
    renderAlerts(data);
    renderWarnings(data.meta?.errors || []);
    return data;
  } catch (error) {
    showError(error.message || "Could not run alert rule");
    throw error;
  } finally {
    setLoading(false);
  }
}

function openSavedAlertRun(row) {
  setMode("alerts", { clear: false });
  clearOutput();
  const payload = row.payload || {
    alerts: row.alerts || [],
    digest: row.digest || {},
    rows: row.rows || [],
    meta: {
      alert_count: row.alert_count || 0,
      high_alert_count: row.high_alert_count || 0,
    },
  };
  state.lastExport = payload;
  exportButton.disabled = false;
  researchLibrary.setCanSave(true);
  sourceChip.textContent = row.trigger || "saved";
  renderAlerts(payload);
}

function watchlistActionPayload(row) {
  const tickers = Array.isArray(row.tickers) ? row.tickers : [];
  return {
    ticker: tickers[0] || "AAPL",
    tickers,
    watchlist_url: row.source_url || "",
    max_results: Number(row.max_results || row.metadata?.max_results || 10),
    period: row.period || row.metadata?.period || "1y",
  };
}

function alertRulePayload(row) {
  return {
    ...watchlistActionPayload(row),
    max_alerts: Number(row.max_alerts || 12),
    volatility_threshold: Number(row.volatility_threshold || 0.55),
  };
}

function showError(message) {
  emptyState.hidden = true;
  errorEl.textContent = message;
  errorEl.hidden = false;
}

function setLoading(isLoading) {
  document.body.classList.toggle("is-loading", isLoading);
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

function buildResearchRecord() {
  if (!state.lastExport) {
    return null;
  }
  const payload = state.lastExport;
  const ticker = researchTicker(payload) || payloadFromForm().ticker;
  return {
    mode: state.mode,
    ticker,
    title: `${modeTitles[state.mode]}${ticker ? ` - ${ticker}` : ""}`,
    summary: modeContracts[state.mode],
    source_url: payload.watchlist?.source_url || payload.meta?.watchlist_source_url,
    payload,
  };
}

function openSavedResearch(record) {
  const payload = record.payload || {};
  const savedModeMap = {
    "watchlist-cockpit": "cockpit",
    "watchlist-alerts": "alerts",
  };
  const savedMode = savedModeMap[record.mode] || record.mode;
  setMode(modeTitles[savedMode] ? savedMode : state.mode, { clear: false });
  clearOutput();
  state.lastExport = payload;
  exportButton.disabled = false;
  researchLibrary.setCanSave(true);
  sourceChip.textContent = "saved";
  if (state.mode === "alerts" || payload.alerts || payload.digest) {
    renderAlerts(payload);
    renderWarnings(payload.meta?.errors || []);
    return;
  }
  if (state.mode === "cockpit" || payload.rows) {
    renderCockpit(payload);
    renderWarnings(payload.meta?.errors || []);
    return;
  }
  if (state.mode === "analysis" || payload.summaries || payload["Anthropic Brief"]) {
    renderAnalysis(payload);
    renderWarnings(payload.meta?.errors || []);
    return;
  }
  renderImages(payload.images || []);
  renderChartExtras(payload);
  renderSummary(payload.meta || {});
  renderWarnings(payload.meta?.errors || []);
}

function researchTicker(payload) {
  return firstString(
    payload.ticker,
    payload.Ticker,
    payload.meta?.ticker,
    payload.tickers?.[0],
    payload.meta?.tickers?.[0],
    payload.summaries?.[0]?.ticker,
    payload.alerts?.[0]?.ticker,
  );
}

function firstString(...values) {
  return values.find((value) => typeof value === "string" && value.trim()) || "";
}

boot();
