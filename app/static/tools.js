import { mountAccountControls, mountResearchLibrary } from "./research.js";

const toolConfig = {
  vision: {
    title: "Vision",
    label: "Reclassification Memo",
    icon: "/static/assets/vision.png",
    action: "See The Vision",
    endpoint: "/api/tools/vision/v2",
    streamEndpoint: "/api/tools/vision/v2/stream",
    classicEndpoint: "/api/tools/vision",
    classicStreamEndpoint: "/api/tools/vision/stream",
    pdfEndpoint: "/api/tools/vision/v2/pdf",
    fields: ["ticker"],
    copy:
      "Old-noun / new-verb reclassification memo: multi-quarter XBRL trend, Exa web research, torque math, scenario targets, kill criteria, proof-ladder stage. Toggle Classic to use the v1 brief.",
  },
  pixel: {
    title: "Pixel",
    label: "Image Generator",
    icon: "/static/assets/toro.png",
    action: "Generate Image",
    endpoint: "/api/tools/pixel",
    fields: ["prompt"],
    copy:
      "Turns a prompt into an 8-bit market visual; use it for branded idea boards and thesis graphics.",
  },
  fax: {
    title: "Stock Fax",
    label: "Stock Analysis",
    icon: "/static/assets/fax.png",
    action: "Get Stock Fax",
    endpoint: "/api/tools/fax",
    fields: ["ticker"],
    copy:
      "Condenses price, trend, volatility, auction levels, and fundamentals into one stock fax for fast triage.",
  },
  moneyline: {
    title: "Moneyline",
    label: "Options Map",
    icon: "/static/assets/moneyline.png",
    action: "View Moneyline",
    endpoint: "/api/tools/moneyline",
    fields: ["ticker", "expiry"],
    copy:
      "Maps option open-interest clusters around spot; use strike walls as positioning pressure, not price targets.",
  },
};

const pathKey = location.pathname.replace("/", "") || "vision";
const activeTool = toolConfig[pathKey] || toolConfig.vision;

const PHASE_DEFINITIONS = [
  { id: "profile", label: "Profile" },
  { id: "sec", label: "SEC filings" },
  { id: "trend", label: "XBRL trend" },
  { id: "exa", label: "Exa research" },
  { id: "torque", label: "Torque" },
  { id: "reclass", label: "Reclassification" },
  { id: "memo", label: "Memo drafting" },
  { id: "verify", label: "Citations" },
];
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
const memoPdfButton = createMemoPdfButton();
const classicToggle = createClassicToggle();
const memoChartViewer = createMemoChartViewer();
let useClassicVision = false;
let lastMemoText = "";
let lastMemoReport = null;
mountAccountControls({ root: document.querySelector("#account-control") });
const researchLibrary = mountResearchLibrary({
  insertAfter: document.querySelector("#tool-form .form-actions"),
  getRecord: buildToolResearchRecord,
  modeFilter: pathKey,
  openRecord: openSavedToolResearch,
});
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
    if (pathKey === "vision") {
      prepareResearchPacketPrint({ focus: true });
    }
    window.print();
  }
});

memoPdfButton?.addEventListener("click", async () => {
  if (!lastMemoText && !lastMemoReport) {
    return;
  }
  memoPdfButton.disabled = true;
  const originalLabel = memoPdfButton.textContent;
  memoPdfButton.textContent = "Rendering...";
  try {
    const ticker = String(toolPayload().ticker || "").toUpperCase();
    const body = JSON.stringify({
      ticker,
      memo_text: lastMemoText,
      report: lastMemoReport,
    });
    const response = await fetch("/api/tools/vision/v2/pdf", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body,
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || "Memo PDF failed");
    }
    const blob = await response.blob();
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${ticker || "vision"}-vision-memo.pdf`;
    document.body.append(link);
    link.click();
    link.remove();
    URL.revokeObjectURL(url);
  } catch (error) {
    showError(error.message || "Memo PDF failed");
  } finally {
    memoPdfButton.textContent = originalLabel;
    memoPdfButton.disabled = !lastMemoText && !lastMemoReport;
  }
});

classicToggle?.addEventListener("change", (event) => {
  useClassicVision = Boolean(event.target.checked);
});

document.addEventListener("keydown", (event) => {
  if (event.key === "Escape" && !memoChartViewer.root.hidden) {
    closeMemoChartViewer();
  }
});

window.addEventListener("afterprint", () => {
  document.body.classList.remove("printing-research-packet");
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
  button.textContent = pathKey === "vision" ? "Packet PDF" : "Export PDF";
  button.disabled = true;
  exportButton.after(button);
  return button;
}

function createMemoPdfButton() {
  if (pathKey !== "vision") {
    return null;
  }
  const button = document.createElement("button");
  button.className = "export-button pdf-button memo-pdf-button";
  button.id = "tool-export-memo-pdf";
  button.type = "button";
  button.textContent = "Memo PDF";
  button.title = "Download a styled analyst-memo PDF rendered server-side";
  button.disabled = true;
  pdfButton.after(button);
  return button;
}

function createClassicToggle() {
  if (pathKey !== "vision") {
    return null;
  }
  const label = document.createElement("label");
  label.className = "classic-toggle";
  label.title = "Use the legacy 10-section Vision memo instead of the v2 reclassification memo";
  const input = document.createElement("input");
  input.type = "checkbox";
  input.id = "vision-classic-toggle";
  const text = document.createElement("span");
  text.textContent = "Classic";
  label.append(input, text);
  const formActions = document.querySelector("#tool-form .form-actions");
  if (formActions) {
    formActions.append(label);
  }
  return input;
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
  const endpoint = useClassicVision
    ? activeTool.classicStreamEndpoint || "/api/tools/vision/stream"
    : activeTool.streamEndpoint || "/api/tools/vision/v2/stream";
  const response = await fetch(endpoint, {
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
    progress: null,
    citations: null,
    elapsedMs: 0,
    tokenCount: 0,
  };

  try {
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
  } catch (error) {
    if (state.progress) {
      state.progress.setError(error?.message || "Stream interrupted");
    }
    throw error;
  } finally {
    if (state.progress && !state.progress.isComplete()) {
      state.progress.stopTicker();
    }
  }
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
    state.progress = mountVisionProgress({
      ticker: event.ticker || toolPayload().ticker || "",
    });
    state.frame = renderVisionFrame(event, { streaming: true });
    updateMemoBody(state.frame.body, "", true);
    emptyEl.hidden = true;
    return;
  }

  if (event.type === "phase") {
    if (state.progress) {
      state.progress.applyPhase(event);
    }
    return;
  }

  if (event.type === "token") {
    state.memo += event.text || "";
    state.tokenCount += 1;
    if (state.progress) {
      state.progress.onToken(state.tokenCount);
    }
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
    state.citations = event.citations || null;
    state.elapsedMs = Number.isFinite(event.elapsed_ms) ? event.elapsed_ms : state.elapsedMs;
    state.tokenCount = Number.isFinite(event.token_count) ? event.token_count : state.tokenCount;
    if (state.frame) {
      updateMemoBody(state.frame.body, state.memo, false, memoCharts(data));
      state.frame.status.textContent = "Complete";
      state.frame.status.classList.add("complete");
    }
    if (state.progress) {
      state.progress.finalize({
        elapsedMs: state.elapsedMs,
        tokenCount: state.tokenCount,
        citations: state.citations,
        sourcesCount: sourceCitationCount(
          visionReport(data)["SEC Source Pack"] || {},
          visionReport(data)["Earnings Source Pack"] || {},
        ),
      });
    }
    if (state.citations && state.frame) {
      mountCitationGauge(state.frame.article, state.citations);
    }
    lastExport = event.export || data;
    lastMemoText = state.memo || "";
    lastMemoReport = event.export?.report || data.Report || data.report || null;
    exportButton.disabled = false;
    pdfButton.disabled = false;
    if (memoPdfButton) {
      memoPdfButton.disabled = !lastMemoText && !lastMemoReport;
    }
    researchLibrary.setCanSave(true);
    sourceEl.textContent = data["Text Model"] || data["Text Provider"] || activeTool.label;
    renderSummary(visionSummaryItems(data));
    return;
  }

  if (event.type === "error") {
    if (state.progress) {
      state.progress.setError(event.error || "Vision stream failed");
    }
    throw new Error(event.error || "Vision stream failed");
  }
}

function renderCompletedResult(data) {
  lastExport = data.export || data;
  exportButton.disabled = false;
  pdfButton.disabled = false;
  researchLibrary.setCanSave(true);
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
  updateMemoBody(frame.body, visionMemo(data), false, memoCharts(data));
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
  heading.textContent = `${visionTicker(data) || "Vision"} Vision`;
  titleGroup.append(eyebrow, heading);

  const meta = document.createElement("div");
  meta.className = "memo-meta";
  meta.append(
    memoChip(visionTextProvider(data) || "anthropic"),
    memoChip(visionTextModel(data) || "model"),
    memoChip(data.provider || visionReport(data).Provider || "market data"),
  );
  const status = memoChip(options.streaming ? "Streaming" : "Complete");
  status.classList.add("memo-status");
  meta.append(status);

  const body = document.createElement("div");
  body.className = "memo-body";

  header.append(titleGroup, meta);
  article.append(header);
  const sourcePanel = visionSourcePanel(data);
  if (sourcePanel) {
    article.append(sourcePanel);
  }
  article.append(body);
  resultEl.append(article);
  return { article, body, status };
}

function visionSummaryItems(data) {
  const report = visionReport(data);
  return [
    ["Ticker", visionTicker(data) || report.Ticker],
    ["Price", report.Snapshot?.Price],
    ["Setup", report["Signal Summary"]?.Setup],
    ["Model", visionTextModel(data) || visionTextProvider(data) || "Generated"],
  ];
}

function memoChip(value) {
  const chip = document.createElement("span");
  chip.className = "memo-chip";
  chip.textContent = formatValue(value);
  return chip;
}

function memoCharts(data) {
  const charts =
    data["Memo Charts"] || data.memo_charts || data.charts || data.export?.memo_charts || [];
  return Array.isArray(charts) ? charts : [];
}

function visionSourcePanel(data) {
  const report = visionReport(data);
  const coverage = report["Data Coverage"] || {};
  const earnings = report["Earnings Source Pack"] || {};
  const sec = report["SEC Source Pack"] || {};
  if (!Object.keys(coverage).length && !Object.keys(earnings).length && !Object.keys(sec).length) {
    return null;
  }

  const panel = document.createElement("section");
  panel.className = "memo-source-panel";
  const head = document.createElement("div");
  head.className = "memo-source-head";
  const title = document.createElement("span");
  title.className = "memo-eyebrow";
  title.textContent = "Source Coverage";
  const status = document.createElement("strong");
  status.textContent = sourceStatusLine(sec, earnings);
  head.append(title, status);

  const grid = document.createElement("div");
  grid.className = "memo-source-grid";
  [
    ["SEC / MD&A", coverage["SEC Filings / MD&A"]],
    ["Earnings", coverage["Earnings Transcript / Guidance"]],
    ["XBRL Facts", Object.keys(sec["Company Facts"] || {}).length || "N/A"],
    ["Citations", sourceCitationCount(sec, earnings)],
  ].forEach(([label, value]) => grid.append(sourceMetric(label, value)));

  panel.append(head, grid);
  const event = earnings["Latest Earnings Event"];
  if (event && Object.keys(event).length) {
    panel.append(earningsEventBlock(event));
  }
  return panel;
}

function visionReport(data) {
  return data.Report || data.report || data.export?.report || {};
}

function visionMemo(data) {
  return firstString(data["Market Memo"], data.market_memo, data.text, data.export?.market_memo);
}

function visionTicker(data) {
  return firstString(
    data.Ticker,
    data.ticker,
    data.Report?.Ticker,
    data.report?.Ticker,
    data.meta?.ticker,
    data.export?.ticker,
    data.export?.meta?.ticker,
    data.tickers?.[0],
    data.export?.tickers?.[0],
  );
}

function visionTextProvider(data) {
  return firstString(data["Text Provider"], data.text_provider, data.export?.text_provider);
}

function visionTextModel(data) {
  return firstString(data["Text Model"], data.text_model, data.export?.text_model);
}

function chartErrors(data) {
  const errors = data["Chart Errors"] || data.chart_errors || data.export?.chart_errors || [];
  return Array.isArray(errors) ? errors : [];
}

function prepareResearchPacketPrint({ focus = false } = {}) {
  if (!lastExport || pathKey !== "vision") {
    return null;
  }
  const packet = renderResearchPacket(lastExport);
  document.body.classList.add("printing-research-packet");
  if (focus) {
    packet.scrollIntoView({ block: "start", behavior: "smooth" });
    packet.focus({ preventScroll: true });
  }
  return packet;
}

function renderResearchPacket(payload) {
  const data = visionPacketData(payload || {});
  resultEl.querySelector("[data-research-packet]")?.remove();

  const packet = document.createElement("section");
  packet.className = "research-packet";
  packet.dataset.researchPacket = "true";
  packet.tabIndex = -1;
  packet.append(
    packetHeader(data),
    packetCoverageSection(data),
    packetMemoSection(data),
    packetChartsSection(data),
    packetHighlightsSection(data),
    packetSourcesSection(data),
    packetWarningsSection(data),
    packetDiligenceSection(data),
  );
  resultEl.append(packet);
  return packet;
}

function visionPacketData(payload) {
  const report = visionReport(payload);
  const sec = objectOrEmpty(report["SEC Source Pack"]);
  const earnings = objectOrEmpty(report["Earnings Source Pack"]);
  return {
    ticker: visionTicker(payload) || "Vision",
    generatedAt: firstString(
      payload.generated_at,
      payload["Generated At"],
      payload.export?.generated_at,
    ),
    textProvider: visionTextProvider(payload),
    textModel: visionTextModel(payload),
    provider: firstString(payload.provider, report.Provider, payload.export?.provider),
    providerNote: firstString(
      payload.provider_note,
      report["Provider Note"],
      payload.export?.provider_note,
    ),
    memo: visionMemo(payload),
    report,
    charts: memoCharts(payload),
    chartErrors: chartErrors(payload),
    sec,
    earnings,
    coverage: objectOrEmpty(report["Data Coverage"]),
  };
}

function packetHeader(data) {
  const header = document.createElement("header");
  header.className = "packet-header";

  const titleGroup = document.createElement("div");
  const eyebrow = document.createElement("span");
  eyebrow.className = "memo-eyebrow";
  eyebrow.textContent = "Research Packet";
  const title = document.createElement("h2");
  title.textContent = `${data.ticker} Tear Sheet`;
  titleGroup.append(eyebrow, title);

  const meta = document.createElement("dl");
  meta.className = "packet-meta";
  [
    ["Generated", formatPacketDate(data.generatedAt)],
    ["Text Model", data.textModel || data.textProvider || "not supplied"],
    ["Text Provider", data.textProvider || "not supplied"],
    ["Data Provider", data.provider || "not supplied"],
    ["Provider Note", data.providerNote || "not supplied"],
  ].forEach(([label, value]) => meta.append(packetMetaItem(label, value)));

  header.append(titleGroup, meta);
  return header;
}

function packetMetaItem(label, value) {
  const wrapper = document.createElement("div");
  const term = document.createElement("dt");
  term.textContent = label;
  const detail = document.createElement("dd");
  detail.textContent = value;
  wrapper.append(term, detail);
  return wrapper;
}

function packetCoverageSection(data) {
  const section = packetSection("Source Coverage");
  section.append(
    packetMetricGrid([
      ["SEC Status", data.sec.Status || "not supplied"],
      ["Earnings Status", data.earnings.Status || "not supplied"],
      ["XBRL Facts", secFactCount(data.sec)],
      ["Citations", sourceCitationCount(data.sec, data.earnings)],
    ]),
  );

  const coverageRows = packetRows(data.coverage);
  if (coverageRows) {
    section.append(coverageRows);
  } else {
    section.append(packetEmpty("Data Coverage: not supplied"));
  }
  return section;
}

function packetMetricGrid(items) {
  const grid = document.createElement("div");
  grid.className = "packet-metric-grid";
  items.forEach(([label, value]) => {
    const item = document.createElement("div");
    item.className = "packet-metric";
    const labelEl = document.createElement("span");
    labelEl.textContent = label;
    const valueEl = document.createElement("strong");
    valueEl.textContent = formatSourceMetricValue(value);
    item.append(labelEl, valueEl);
    grid.append(item);
  });
  return grid;
}

function packetMemoSection(data) {
  const section = packetSection("Vision Memo");
  const body = document.createElement("div");
  body.className = "packet-memo-body";
  if (data.memo.trim()) {
    renderMarkdown(data.memo, body, []);
  } else {
    body.append(packetEmpty("Vision memo: not supplied"));
  }
  section.append(body);
  return section;
}

function packetChartsSection(data) {
  const section = packetSection("Memo Charts");
  if (!data.charts.length) {
    section.append(packetEmpty("Memo Charts: not supplied"));
    return section;
  }

  const grid = document.createElement("div");
  grid.className = "packet-chart-grid";
  data.charts.forEach((chart) => grid.append(packetChartFigure(chart)));
  section.append(grid);
  return section;
}

function packetChartFigure(chart) {
  const image = chart.image || chart;
  const filename = image.filename || `${chart.key || "memo-chart"}.png`;
  const figure = document.createElement("figure");
  figure.className = "packet-chart";

  const title = document.createElement("figcaption");
  const label = document.createElement("span");
  label.className = "memo-eyebrow";
  label.textContent = chart.placement || "Memo Chart";
  const name = document.createElement("strong");
  name.textContent = chart.title || filename;
  const caption = document.createElement("p");
  caption.textContent = chart.caption || chart.description || filename;
  title.append(label, name, caption);

  if (image.data) {
    const img = document.createElement("img");
    img.src = `data:${image.mime || "image/png"};base64,${image.data}`;
    img.alt = chart.title || filename;
    img.loading = "lazy";
    figure.append(img);
  } else {
    figure.append(packetEmpty(`${chart.title || filename}: image not supplied`));
  }
  figure.append(title);
  return figure;
}

function packetHighlightsSection(data) {
  const section = packetSection("Stock Fax Highlights");
  const grid = document.createElement("div");
  grid.className = "packet-highlight-grid";
  [
    ["Snapshot", data.report.Snapshot],
    ["Signal Summary", data.report["Signal Summary"]],
    ["Auction Levels", data.report["Auction Market Theory Price Levels"]],
    ["Valuation", data.report["Valuation Context"]],
    ["Financial Quality", data.report["Financial Quality"]],
    ["Volatility", data.report["Volatility Metrics"]],
  ].forEach(([title, value]) => grid.append(packetDataBlock(title, value)));
  section.append(grid);
  return section;
}

function packetSourcesSection(data) {
  const section = packetSection("Citations And Earnings Sources");
  const grid = document.createElement("div");
  grid.className = "packet-source-grid";
  grid.append(packetCitationGroup("SEC Citations", data.sec.Citations));
  grid.append(packetEarningsGroup(data.earnings));
  section.append(grid);
  return section;
}

function packetCitationGroup(title, citations) {
  const article = document.createElement("article");
  article.className = "packet-source-block";
  const heading = document.createElement("h3");
  heading.textContent = title;
  article.append(heading);

  const unique = uniqueCitations(citations);
  if (!unique.length) {
    article.append(packetEmpty(`${title}: not supplied`));
    return article;
  }

  const list = document.createElement("ul");
  list.className = "packet-citation-list";
  unique.forEach((citation) => list.append(packetCitationItem(citation)));
  article.append(list);
  return article;
}

function packetEarningsGroup(earnings) {
  const article = document.createElement("article");
  article.className = "packet-source-block";
  const heading = document.createElement("h3");
  heading.textContent = "Earnings Source Pack";
  article.append(heading);

  const event = objectOrEmpty(earnings["Latest Earnings Event"]);
  const eventRows = packetRows(event);
  if (eventRows) {
    const eventBlock = document.createElement("div");
    eventBlock.className = "packet-subblock";
    const eventHeading = document.createElement("h4");
    eventHeading.textContent = "Latest Event";
    eventBlock.append(eventHeading, eventRows);
    article.append(eventBlock);
  } else {
    article.append(packetEmpty("Latest Earnings Event: not supplied"));
  }

  const sections = objectEntries(earnings["SEC 8-K Sections"]);
  if (sections.length) {
    const sectionList = document.createElement("div");
    sectionList.className = "packet-subblock";
    const sectionHeading = document.createElement("h4");
    sectionHeading.textContent = "SEC 8-K Sections";
    sectionList.append(sectionHeading);
    sections.forEach(([label, section]) => {
      const card = document.createElement("div");
      card.className = "packet-source-note";
      const title = document.createElement("strong");
      title.textContent = `${label}: ${formatValue(section.Item || section.Heading)}`;
      const snippet = document.createElement("p");
      snippet.textContent = compactText(section.Snippet || section.Summary, 320);
      card.append(title, snippet);
      sectionList.append(card);
    });
    article.append(sectionList);
  }

  article.append(packetCitationGroup("Earnings Citations", earnings.Citations));
  return article;
}

function packetWarningsSection(data) {
  const section = packetSection("Warnings And Errors");
  const warnings = visionWarnings(data);
  if (!warnings.length) {
    section.append(packetEmpty("Warnings: none reported"));
    return section;
  }

  const list = document.createElement("ul");
  list.className = "packet-warning-list";
  warnings.forEach((warning) => {
    const item = document.createElement("li");
    item.textContent = warning;
    list.append(item);
  });
  section.append(list);
  return section;
}

function packetDiligenceSection(data) {
  const section = packetSection("Next Diligence");
  const items = diligenceItems(data.coverage);
  const list = document.createElement("ul");
  list.className = "packet-checklist";
  items.forEach((item) => {
    const row = document.createElement("li");
    row.textContent = item;
    list.append(row);
  });
  section.append(list);
  return section;
}

function packetSection(title) {
  const section = document.createElement("section");
  section.className = "packet-section";
  const heading = document.createElement("h3");
  heading.textContent = title;
  section.append(heading);
  return section;
}

function packetDataBlock(title, value) {
  const article = document.createElement("article");
  article.className = "packet-data-block";
  const heading = document.createElement("h4");
  heading.textContent = title;
  article.append(heading);
  const rows = packetRows(value);
  article.append(rows || packetEmpty(`${title}: not supplied`));
  return article;
}

function packetRows(value) {
  const entries = objectEntries(value);
  if (!entries.length) {
    return null;
  }

  const rows = document.createElement("dl");
  rows.className = "packet-rows";
  entries.forEach(([label, item]) => {
    const term = document.createElement("dt");
    term.textContent = label;
    const detail = document.createElement("dd");
    detail.textContent = formatPacketValue(item);
    rows.append(term, detail);
  });
  return rows;
}

function packetCitationItem(citation) {
  const item = document.createElement("li");
  item.className = "packet-citation";
  const label = document.createElement("strong");
  label.textContent =
    firstString(citation.Label, citation.Form, citation.Type, citation.Provider) || "Citation";
  item.append(label);

  const meta = [
    citation.Type,
    citation.Form,
    citation["Filing Date"],
    citation.Provider,
  ].filter(Boolean);
  if (meta.length) {
    const metaEl = document.createElement("span");
    metaEl.textContent = meta.map((value) => formatValue(value)).join(" / ");
    item.append(metaEl);
  }

  const url = firstString(citation.URL, citation["Source URL"]);
  if (url) {
    const link = document.createElement("a");
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    link.textContent = url;
    item.append(link);
  } else {
    const missing = document.createElement("span");
    missing.textContent = "URL: not supplied";
    item.append(missing);
  }
  return item;
}

function packetEmpty(message) {
  const empty = document.createElement("p");
  empty.className = "packet-empty";
  empty.textContent = message;
  return empty;
}

function visionWarnings(data) {
  const warnings = [];
  data.chartErrors.forEach((error) => {
    warnings.push(`Chart Error: ${chartErrorText(error)}`);
  });
  listOf(data.sec.Errors).forEach((error) =>
    warnings.push(`SEC Source Pack: ${formatValue(error)}`),
  );
  listOf(data.earnings.Errors).forEach((error) =>
    warnings.push(`Earnings Source Pack: ${formatValue(error)}`),
  );
  objectEntries(data.coverage)
    .filter(([, status]) => isCoverageGap(status))
    .forEach(([label, status]) => warnings.push(`${label}: ${formatValue(status)}`));

  if (!data.memo.trim()) {
    warnings.push("Vision memo: not supplied");
  }
  if (!data.charts.length) {
    warnings.push("Memo Charts: not supplied");
  }
  if (!data.sec.Status) {
    warnings.push("SEC Source Pack: not supplied");
  }
  if (!data.earnings.Status) {
    warnings.push("Earnings Source Pack: not supplied");
  }

  return [...new Set(warnings.filter(Boolean))];
}

function diligenceItems(coverage) {
  const entries = objectEntries(coverage);
  if (!entries.length) {
    return ["Data Coverage: not supplied"];
  }

  const gaps = entries.filter(([, status]) => isCoverageGap(status));
  if (!gaps.length) {
    return ["No partial or missing coverage items flagged by Data Coverage."];
  }
  return gaps.map(([label, status]) => `Resolve ${label} coverage (${formatValue(status)}).`);
}

function isCoverageGap(value) {
  return /partial|not supplied|unavailable|not configured/i.test(String(value || ""));
}

function chartErrorText(error) {
  if (typeof error === "string") {
    return error;
  }
  if (error && typeof error === "object") {
    const name = firstString(error.chart, error.key, error.title, error.placement);
    const message = firstString(error.error, error.message, error.reason) || formatValue(error);
    return name ? `${name}: ${message}` : message;
  }
  return formatValue(error);
}

function uniqueCitations(citations) {
  const seen = new Set();
  return listOf(citations)
    .filter((citation) => citation && typeof citation === "object")
    .filter((citation) => {
      const key = [
        citation.Label,
        citation.Type,
        citation.Form,
        citation["Filing Date"],
        citation.URL,
        citation["Source URL"],
      ]
        .filter(Boolean)
        .join(":");
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    });
}

function secFactCount(sec) {
  return objectEntries(sec["Company Facts"]).length;
}

function formatPacketDate(value) {
  if (!value) {
    return "not supplied";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return String(value);
  }
  return date.toLocaleString(undefined, {
    year: "numeric",
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function formatPacketValue(value) {
  if (value && typeof value === "object" && !Array.isArray(value)) {
    const entries = objectEntries(value);
    if (entries.length) {
      return entries.map(([label, child]) => `${label}: ${formatValue(child)}`).join(" / ");
    }
  }
  return formatValue(value).replace(/\s+/g, " ");
}

function objectOrEmpty(value) {
  return value && typeof value === "object" && !Array.isArray(value) ? value : {};
}

function objectEntries(value) {
  return Object.entries(objectOrEmpty(value));
}

function listOf(value) {
  return Array.isArray(value) ? value : [];
}

function sourceStatusLine(sec, earnings) {
  const secStatus = sec.Status || "not supplied";
  const earningsStatus = earnings.Status || "not supplied";
  return `SEC ${secStatus} / Earnings ${earningsStatus}`;
}

function sourceCitationCount(sec, earnings) {
  const citations = [
    ...(Array.isArray(sec.Citations) ? sec.Citations : []),
    ...(Array.isArray(earnings.Citations) ? earnings.Citations : []),
  ];
  return new Set(
    citations.map((citation) => `${citation?.Label || ""}:${citation?.URL || ""}`)
  ).size;
}

function sourceMetric(label, value) {
  const item = document.createElement("div");
  item.className = "memo-source-metric";
  const labelEl = document.createElement("span");
  labelEl.textContent = label;
  const valueEl = document.createElement("strong");
  valueEl.textContent = formatSourceMetricValue(value);
  item.append(labelEl, valueEl);
  return item;
}

function formatSourceMetricValue(value) {
  if (Number.isInteger(value)) {
    return String(value);
  }
  return formatValue(value);
}

function earningsEventBlock(event) {
  const block = document.createElement("div");
  block.className = "memo-source-event";
  const title = document.createElement("strong");
  title.textContent = [event.Type, event.Item, event["Filing Date"] || event["Next Earnings Date"]]
    .filter(Boolean)
    .join(" / ");
  block.append(title);
  const summary = event.Summary;
  if (summary) {
    const paragraph = document.createElement("p");
    paragraph.textContent = compactText(summary, 260);
    block.append(paragraph);
  }
  return block;
}

function compactText(value, limit) {
  const text = String(value || "").replace(/\s+/g, " ").trim();
  if (text.length <= limit) {
    return text;
  }
  return `${text.slice(0, limit - 1).trim()}...`;
}

function updateMemoBody(body, markdown, streaming, charts = []) {
  body.innerHTML = "";
  if (markdown.trim()) {
    renderMarkdown(markdown, body, charts);
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
  const visible = items
    .map(([label, value]) => [label, formatValue(value)])
    .filter(([_label, formatted]) => formatted !== "N/A" && formatted !== "" && formatted !== "None");
  visible.forEach(([label, formatted]) => summaryEl.append(summaryItem(label, formatted)));
  summaryEl.hidden = visible.length === 0;
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

function renderMarkdown(markdown, container, charts = []) {
  const usedCharts = new Set();
  markdownBlocks(markdown).forEach((node) => {
    container.append(node);
    if (node instanceof HTMLHeadingElement) {
      charts
        .map((chart, index) => [chart, index])
        .filter(([chart, index]) => !usedCharts.has(index) && chartBelongsAfterHeading(chart, node))
        .forEach(([chart, index]) => {
          container.append(memoChartFigure(chart));
          usedCharts.add(index);
        });
    }
  });
  charts.forEach((chart, index) => {
    if (!usedCharts.has(index)) {
      container.append(memoChartFigure(chart));
    }
  });
}

function chartBelongsAfterHeading(chart, heading) {
  const placement = String(chart.placement || "").toLowerCase();
  const title = String(chart.title || chart.key || "").toLowerCase();
  const text = heading.textContent.toLowerCase();
  return (
    (placement && (text.includes(placement) || placement.includes(text))) ||
    (title.includes("auction") && text.includes("price map")) ||
    (title.includes("regression") && text.includes("performance")) ||
    (title.includes("volatility") && (text.includes("risk") || text.includes("variant")))
  );
}

function memoChartFigure(chart) {
  const image = chart.image || chart;
  const src = `data:${image.mime};base64,${image.data}`;
  const filename = image.filename || `${chart.key || "memo-chart"}.png`;

  const figure = document.createElement("figure");
  figure.className = "memo-chart";

  const head = document.createElement("div");
  head.className = "memo-chart-head";
  const titleGroup = document.createElement("div");
  const label = document.createElement("span");
  label.className = "memo-eyebrow";
  label.textContent = chart.placement || "Evidence";
  const title = document.createElement("h4");
  title.textContent = chart.title || filename;
  titleGroup.append(label, title);

  const actions = document.createElement("div");
  actions.className = "chart-actions memo-chart-actions";
  actions.append(
    chartActionButton("Inspect", () => openMemoChartViewer({ src, filename })),
    chartActionLink("Open PNG", src, filename, false),
    chartActionLink("Download", src, filename, true),
  );
  head.append(titleGroup, actions);

  const preview = document.createElement("button");
  preview.className = "chart-preview-button memo-chart-preview";
  preview.type = "button";
  preview.setAttribute("aria-label", `Inspect ${chart.title || filename}`);
  const img = document.createElement("img");
  img.src = src;
  img.alt = chart.title || filename;
  preview.append(img);
  preview.addEventListener("click", () => openMemoChartViewer({ src, filename }));

  const caption = document.createElement("figcaption");
  caption.textContent = chart.caption || chart.description || filename;

  figure.append(head, preview, caption);
  return figure;
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

function createMemoChartViewer() {
  const root = document.createElement("div");
  root.className = "chart-viewer";
  root.hidden = true;
  root.setAttribute("role", "dialog");
  root.setAttribute("aria-modal", "true");
  root.setAttribute("aria-labelledby", "memo-chart-viewer-title");

  const panel = document.createElement("div");
  panel.className = "chart-viewer-panel";

  const head = document.createElement("div");
  head.className = "chart-viewer-head";

  const titleGroup = document.createElement("div");
  const label = document.createElement("div");
  label.className = "panel-label";
  label.textContent = "Memo Chart";
  const title = document.createElement("h2");
  title.id = "memo-chart-viewer-title";
  title.textContent = "Memo Chart";
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
  closeButton.addEventListener("click", closeMemoChartViewer);
  actions.append(openLink, downloadLink, closeButton);

  const imageWrap = document.createElement("div");
  imageWrap.className = "chart-viewer-image-wrap";
  const image = document.createElement("img");
  image.className = "chart-viewer-image";
  image.alt = "Expanded memo chart";
  imageWrap.append(image);

  head.append(titleGroup, actions);
  panel.append(head, imageWrap);
  root.append(panel);
  root.addEventListener("click", (event) => {
    if (event.target === root) {
      closeMemoChartViewer();
    }
  });
  document.body.append(root);
  return { root, title, image, openLink, downloadLink, closeButton, previousFocus: null };
}

function openMemoChartViewer({ src, filename }) {
  memoChartViewer.previousFocus = document.activeElement;
  memoChartViewer.title.textContent = filename;
  memoChartViewer.image.src = src;
  memoChartViewer.image.alt = filename;
  memoChartViewer.openLink.href = src;
  memoChartViewer.downloadLink.href = src;
  memoChartViewer.downloadLink.download = filename;
  memoChartViewer.root.hidden = false;
  document.body.classList.add("chart-viewer-open");
  memoChartViewer.closeButton.focus();
}

function closeMemoChartViewer() {
  memoChartViewer.root.hidden = true;
  document.body.classList.remove("chart-viewer-open");
  if (memoChartViewer.previousFocus?.focus) {
    memoChartViewer.previousFocus.focus();
  }
  memoChartViewer.previousFocus = null;
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
  if (lines.length && lines.every((line) => /^[-*]\s+/.test(line))) {
    return [markdownList(lines, "ul")];
  }
  if (lines.length && lines.every((line) => /^\d+\.\s+/.test(line))) {
    return [markdownList(lines, "ol")];
  }
  if (lines.length && lines.every((line) => /^>\s?/.test(line))) {
    const quote = document.createElement("blockquote");
    quote.className = "memo-blockquote";
    lines.forEach((line, index) => {
      if (index > 0) {
        quote.append(document.createElement("br"));
      }
      appendInlineMarkdown(quote, line.replace(/^>\s?/, ""));
    });
    return [quote];
  }
  if (block.startsWith("### ")) {
    const heading = document.createElement("h3");
    heading.textContent = block.replace(/^###\s+/, "");
    return [heading];
  }
  if (block.startsWith("## ")) {
    const heading = document.createElement("h2");
    heading.textContent = block.replace(/^##\s+/, "");
    return [heading];
  }
  if (block.startsWith("# ")) {
    const heading = document.createElement("h1");
    heading.textContent = block.replace(/^#\s+/, "");
    return [heading];
  }
  if (/^\s*---+\s*$/.test(block)) {
    return [document.createElement("hr")];
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

function markdownList(lines, kind = "ul") {
  const list = document.createElement(kind);
  list.className = kind === "ol" ? "memo-ol" : "memo-ul";
  lines.forEach((line) => {
    const item = document.createElement("li");
    const stripped = kind === "ol" ? line.replace(/^\d+\.\s+/, "") : line.replace(/^[-*]\s+/, "");
    appendInlineMarkdown(item, stripped);
    list.append(item);
  });
  return list;
}

function appendInlineMarkdown(parent, text) {
  const pattern = /(\*\*([^*]+?)\*\*)|(`([^`]+?)`)|(\*([^*]+?)\*)|(_([^_]+?)_)/g;
  let cursor = 0;
  let match = pattern.exec(text);
  while (match) {
    if (match.index > cursor) {
      parent.append(document.createTextNode(text.slice(cursor, match.index)));
    }
    if (match[2] !== undefined) {
      const strong = document.createElement("strong");
      strong.textContent = match[2];
      parent.append(strong);
    } else if (match[4] !== undefined) {
      const code = document.createElement("code");
      code.className = "memo-inline-code";
      code.textContent = match[4];
      parent.append(code);
    } else if (match[6] !== undefined) {
      const em = document.createElement("em");
      em.textContent = match[6];
      parent.append(em);
    } else if (match[8] !== undefined) {
      const em = document.createElement("em");
      em.textContent = match[8];
      parent.append(em);
    }
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
  closeMemoChartViewer();
  document.body.classList.remove("printing-research-packet");
  resultEl.innerHTML = "";
  summaryEl.innerHTML = "";
  summaryEl.hidden = true;
  errorEl.hidden = true;
  errorEl.textContent = "";
  sourceEl.textContent = "idle";
  emptyEl.hidden = false;
  const progressSlot = document.querySelector("#vision-progress-slot");
  if (progressSlot) {
    progressSlot.innerHTML = "";
  }
  lastExport = null;
  exportButton.disabled = true;
  pdfButton.disabled = true;
  researchLibrary.setCanSave(false);
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

function buildToolResearchRecord() {
  if (!lastExport) {
    return null;
  }
  const ticker = toolResearchTicker(lastExport) || toolPayload().ticker || "";
  return {
    mode: pathKey,
    ticker,
    title: `${activeTool.title}${ticker ? ` - ${ticker}` : ""}`,
    summary: activeTool.copy,
    payload: lastExport,
  };
}

function openSavedToolResearch(record) {
  clearOutput();
  lastExport = record.payload || {};
  exportButton.disabled = false;
  pdfButton.disabled = false;
  researchLibrary.setCanSave(true);
  sourceEl.textContent = "saved";
  renderToolResult(lastExport);
}

function toolResearchTicker(payload) {
  return firstString(
    payload.Ticker,
    payload.ticker,
    payload.Report?.Ticker,
    payload.meta?.ticker,
    payload.export?.ticker,
  );
}

function firstString(...values) {
  return values.find((value) => typeof value === "string" && value.trim()) || "";
}

/* ============================================================
 * Vision progress card
 * ============================================================ */

function mountVisionProgress({ ticker }) {
  const slot = document.querySelector("#vision-progress-slot");
  if (!slot) {
    return null;
  }
  slot.innerHTML = "";

  const phases = PHASE_DEFINITIONS.map((p) => ({
    ...p,
    status: "pending",
    elapsedMs: null,
  }));

  const root = document.createElement("section");
  root.className = "vision-progress";
  root.dataset.state = "running";

  const head = document.createElement("div");
  head.className = "vision-progress-head";
  const titleWrap = document.createElement("div");
  titleWrap.className = "vision-progress-title";
  const eyebrow = document.createElement("span");
  eyebrow.textContent = "Market Memo";
  const tickerEl = document.createElement("strong");
  tickerEl.textContent = ticker ? `· ${String(ticker).toUpperCase()}` : "";
  titleWrap.append(eyebrow, tickerEl);

  const stats = document.createElement("div");
  stats.className = "vision-progress-stats";
  const elapsedEl = document.createElement("span");
  elapsedEl.className = "elapsed";
  elapsedEl.textContent = "0s";
  const tokensEl = document.createElement("span");
  tokensEl.className = "tokens";
  tokensEl.hidden = true;
  stats.append(elapsedEl, tokensEl);
  head.append(titleWrap, stats);

  const rail = document.createElement("div");
  rail.className = "vision-progress-rail";
  const railFill = document.createElement("div");
  railFill.className = "vision-progress-rail-fill";
  rail.append(railFill);

  const chips = document.createElement("div");
  chips.className = "vision-progress-chips";
  const chipMap = new Map();
  phases.forEach((phase) => {
    const chip = document.createElement("span");
    chip.className = "vision-progress-chip";
    chip.dataset.status = "pending";
    chip.dataset.phase = phase.id;
    const icon = document.createElement("span");
    icon.className = "vision-progress-chip-icon";
    const label = document.createElement("span");
    label.textContent = phase.label;
    chip.append(icon, label);
    chips.append(chip);
    chipMap.set(phase.id, chip);
  });

  const callout = document.createElement("div");
  callout.className = "vision-progress-callout";
  const calloutLabel = document.createElement("span");
  calloutLabel.className = "vision-progress-callout-label";
  calloutLabel.textContent = "Booting Vision pipeline...";
  const calloutMeta = document.createElement("span");
  calloutMeta.className = "vision-progress-callout-meta";
  callout.append(calloutLabel, calloutMeta);

  // Collapsed summary bar (hidden until finalize).
  const summaryBar = document.createElement("button");
  summaryBar.type = "button";
  summaryBar.className = "vision-progress-summary-bar";
  const summaryStatus = document.createElement("span");
  summaryStatus.className = "vision-progress-summary-status";
  summaryStatus.textContent = "✓ Complete";
  const summaryText = document.createElement("span");
  summaryText.className = "vision-progress-summary-text";
  const summaryChevron = document.createElement("span");
  summaryChevron.className = "vision-progress-summary-chevron";
  summaryChevron.textContent = "▾";
  summaryBar.append(summaryStatus, summaryText, summaryChevron);
  summaryBar.addEventListener("click", () => {
    const expanded = root.dataset.expanded === "true";
    root.dataset.expanded = expanded ? "false" : "true";
  });

  root.append(summaryBar, head, rail, chips, callout);
  slot.append(root);

  const startedAt = Date.now();
  let currentPhaseId = null;
  let phaseStartedAt = startedAt;
  let targetProgress = 0;
  let displayedProgress = 0;
  let railRafId = null;
  let tokensSeen = 0;
  let complete = false;
  let errored = false;

  const tickerInterval = window.setInterval(() => {
    if (complete || errored) {
      return;
    }
    const seconds = Math.floor((Date.now() - startedAt) / 1000);
    elapsedEl.textContent = `${seconds}s`;
    updateCalloutMeta();
  }, 1000);

  function stopTicker() {
    window.clearInterval(tickerInterval);
  }

  function updateCalloutMeta() {
    if (!currentPhaseId || complete) {
      calloutMeta.textContent = "";
      return;
    }
    const inStage = ((Date.now() - phaseStartedAt) / 1000).toFixed(1);
    const tokenSuffix =
      currentPhaseId === "memo" && tokensSeen > 0
        ? `${tokensSeen.toLocaleString()} tokens · `
        : "";
    calloutMeta.textContent = `${tokenSuffix}${inStage}s in this stage`;
  }

  function scheduleRailUpdate() {
    if (railRafId !== null) {
      return;
    }
    railRafId = window.requestAnimationFrame(() => {
      railRafId = null;
      displayedProgress = targetProgress;
      railFill.style.width = `${Math.max(0, Math.min(100, displayedProgress * 100))}%`;
    });
  }

  function setActivePhase(phaseId, label) {
    const idx = phases.findIndex((p) => p.id === phaseId);
    if (idx === -1) {
      return;
    }
    phases.forEach((phase, i) => {
      if (i < idx) {
        phase.status = "done";
      } else if (i === idx) {
        phase.status = "active";
      } else {
        phase.status = "pending";
      }
      const chip = chipMap.get(phase.id);
      if (chip) {
        chip.dataset.status = phase.status;
      }
    });
    currentPhaseId = phaseId;
    phaseStartedAt = Date.now();
    calloutLabel.textContent = label || phases[idx].label || "Working...";
    updateCalloutMeta();
  }

  function applyPhase(event) {
    if (!event || !event.phase_id) {
      return;
    }
    setActivePhase(event.phase_id, event.label);
    if (Number.isFinite(event.progress)) {
      targetProgress = Math.max(targetProgress, Number(event.progress));
      scheduleRailUpdate();
    }
  }

  function onToken(count) {
    tokensSeen = count;
    tokensEl.hidden = false;
    tokensEl.textContent = `${count.toLocaleString()} tk`;
    // If the backend never emitted a `memo` phase before tokens arrived,
    // promote the memo phase ourselves so the chip lights up.
    if (currentPhaseId !== "memo" && currentPhaseId !== "verify") {
      setActivePhase("memo", "Drafting analyst memo...");
      targetProgress = Math.max(targetProgress, 0.8);
      scheduleRailUpdate();
    } else {
      updateCalloutMeta();
    }
  }

  function finalize({ elapsedMs, tokenCount, citations, sourcesCount }) {
    complete = true;
    stopTicker();
    phases.forEach((phase) => {
      phase.status = "done";
      const chip = chipMap.get(phase.id);
      if (chip) {
        chip.dataset.status = "done";
      }
    });
    targetProgress = 1;
    scheduleRailUpdate();
    root.dataset.state = "done";
    const seconds = Math.max(1, Math.round((elapsedMs || Date.now() - startedAt) / 1000));
    elapsedEl.textContent = `${seconds}s`;
    if (Number.isFinite(tokenCount) && tokenCount > 0) {
      tokensEl.hidden = false;
      tokensEl.textContent = `${tokenCount.toLocaleString()} tk`;
    }
    calloutLabel.textContent = "Complete";
    calloutMeta.textContent = "";

    const parts = [`${seconds}s`];
    if (Number.isFinite(tokenCount) && tokenCount > 0) {
      parts.push(`${tokenCount.toLocaleString()} tokens`);
    }
    if (Number.isFinite(sourcesCount) && sourcesCount > 0) {
      parts.push(`${sourcesCount} sources`);
    }
    if (citations && Number.isFinite(citations.total) && Number.isFinite(citations.verified)) {
      const checkable = Number.isFinite(citations.checkable) ? citations.checkable : citations.total;
      parts.push(`${citations.verified}/${checkable} citations verified`);
    }
    summaryText.textContent = parts.join(" · ");
    root.dataset.collapsed = "true";
    root.dataset.expanded = "false";
  }

  function setError(message) {
    errored = true;
    stopTicker();
    root.dataset.state = "error";
    calloutLabel.textContent = message || "Vision stream failed";
    calloutMeta.textContent = "";
  }

  return {
    root,
    applyPhase,
    onToken,
    finalize,
    setError,
    stopTicker,
    isComplete: () => complete,
  };
}

/* ============================================================
 * Citation verification gauge
 * ============================================================ */

function mountCitationGauge(memoArticle, citations) {
  if (!memoArticle || !citations) {
    return null;
  }
  // Remove any previous gauge before mounting (e.g. if user clicks Generate twice).
  memoArticle.querySelector(".citation-gauge")?.remove();

  const total = Number.isFinite(citations.total) ? citations.total : 0;
  const checkable = Number.isFinite(citations.checkable) ? citations.checkable : total;
  const verified = Number.isFinite(citations.verified) ? citations.verified : 0;
  const percent =
    Number.isFinite(citations.percent_verified)
      ? citations.percent_verified
      : checkable > 0
        ? verified / checkable
        : 0;
  const checks = Array.isArray(citations.checks) ? citations.checks : [];

  const counts = checks.reduce(
    (acc, check) => {
      const status = String(check?.status || "uncheckable");
      if (status === "verified") acc.verified += 1;
      else if (status === "value_mismatch") acc.mismatch += 1;
      else if (status === "concept_missing" || status === "missing") acc.missing += 1;
      else acc.uncheckable += 1;
      return acc;
    },
    { verified: 0, mismatch: 0, missing: 0, uncheckable: 0 },
  );
  // If counts derived from checks are zero but totals were given, fall back.
  if (!checks.length && verified) {
    counts.verified = verified;
  }

  const gauge = document.createElement("section");
  gauge.className = "citation-gauge";
  gauge.dataset.expanded = "false";
  gauge.dataset.quality = percent >= 0.85 ? "good" : percent >= 0.6 ? "warn" : "poor";

  const head = document.createElement("div");
  head.className = "citation-gauge-head";
  const label = document.createElement("span");
  label.className = "citation-gauge-label";
  label.textContent = "Citations";
  const countsEl = document.createElement("span");
  countsEl.className = "citation-gauge-counts";
  countsEl.textContent = `${verified} / ${checkable}`;
  const percentEl = document.createElement("span");
  percentEl.className = "percent";
  percentEl.textContent = `${Math.round(percent * 100)}%`;
  countsEl.append(percentEl);
  head.append(label, countsEl);

  const bar = document.createElement("div");
  bar.className = "citation-gauge-bar";
  const barFill = document.createElement("div");
  barFill.className = "citation-gauge-bar-fill";
  barFill.style.width = `${Math.max(0, Math.min(100, percent * 100))}%`;
  bar.append(barFill);

  const legend = document.createElement("div");
  legend.className = "citation-gauge-legend";
  legend.append(
    legendEntry("verified", `✓ ${counts.verified} verified`),
    legendEntry("mismatch", `⚠ ${counts.mismatch} value mismatch`),
    legendEntry("missing", `✕ ${counts.missing} missing`),
    legendEntry("uncheckable", `◌ ${counts.uncheckable} uncheckable`),
  );

  const toggle = document.createElement("button");
  toggle.type = "button";
  toggle.className = "citation-gauge-toggle";
  toggle.textContent = "View per-citation detail ▾";

  const detail = document.createElement("div");
  detail.className = "citation-gauge-detail";
  detail.append(citationGaugeTable(checks, memoArticle));

  toggle.addEventListener("click", () => {
    const expanded = gauge.dataset.expanded === "true";
    gauge.dataset.expanded = expanded ? "false" : "true";
    toggle.textContent = expanded ? "View per-citation detail ▾" : "Hide per-citation detail ▴";
  });

  gauge.append(head, bar, legend, toggle, detail);

  // Mount just above the memo body so it lives inside the memo card header area.
  const body = memoArticle.querySelector(".memo-body");
  if (body) {
    memoArticle.insertBefore(gauge, body);
  } else {
    memoArticle.append(gauge);
  }
  return gauge;
}

function legendEntry(cls, text) {
  const el = document.createElement("span");
  el.className = cls;
  el.textContent = text;
  return el;
}

function citationGaugeTable(checks, memoArticle) {
  const wrap = document.createElement("div");
  if (!checks.length) {
    const empty = document.createElement("p");
    empty.style.color = "var(--muted)";
    empty.style.fontSize = "0.8rem";
    empty.style.margin = "0.3rem 0 0";
    empty.textContent = "No per-citation detail available.";
    wrap.append(empty);
    return wrap;
  }

  const table = document.createElement("table");
  table.className = "citation-gauge-table";
  const thead = document.createElement("thead");
  thead.innerHTML =
    "<tr><th></th><th>Kind</th><th>Citation</th><th>Matched</th><th>Note</th></tr>";
  const tbody = document.createElement("tbody");

  checks.forEach((check) => {
    const status = String(check?.status || "uncheckable");
    const row = document.createElement("tr");
    row.className = "citation-gauge-row";
    row.dataset.status = status;

    const statusCell = document.createElement("td");
    statusCell.className = "status-cell";
    statusCell.textContent = statusGlyph(status);
    statusCell.dataset.label = "Status";

    const kindCell = document.createElement("td");
    kindCell.className = "kind-cell";
    kindCell.textContent = String(check?.kind || "—");
    kindCell.dataset.label = "Kind";

    const rawCell = document.createElement("td");
    rawCell.className = "raw-cell";
    rawCell.textContent = String(check?.raw || check?.target || "—");
    rawCell.dataset.label = "Citation";

    const matchedCell = document.createElement("td");
    matchedCell.className = "matched-cell";
    matchedCell.textContent = String(check?.matched_value || check?.cited_value || "—");
    matchedCell.dataset.label = "Matched";

    const noteCell = document.createElement("td");
    noteCell.className = "note-cell";
    noteCell.textContent = String(check?.note || "");
    noteCell.dataset.label = "Note";

    row.append(statusCell, kindCell, rawCell, matchedCell, noteCell);
    row.addEventListener("click", () => jumpToCitation(memoArticle, check));
    tbody.append(row);
  });

  table.append(thead, tbody);
  wrap.append(table);
  return wrap;
}

function statusGlyph(status) {
  if (status === "verified") return "✓";
  if (status === "value_mismatch") return "⚠";
  if (status === "concept_missing" || status === "missing") return "✕";
  return "◌";
}

function jumpToCitation(memoArticle, check) {
  const body = memoArticle?.querySelector(".memo-body");
  if (!body || !check) {
    return;
  }
  const needle = String(check.raw || check.target || "").trim();
  if (!needle) {
    return;
  }
  const target = findTextNodeContaining(body, needle);
  if (!target) {
    return;
  }
  const element = target.parentElement || body;
  element.scrollIntoView({ behavior: "smooth", block: "center" });
  element.classList.remove("citation-target-flash");
  // Force reflow so the animation restarts on repeated clicks.
  void element.offsetWidth;
  element.classList.add("citation-target-flash");
}

function findTextNodeContaining(root, needle) {
  // Search by progressively shorter prefixes so the raw citation string still
  // matches even if the rendered memo wrapped it in inline formatting.
  const candidates = [needle, needle.slice(0, 60), needle.slice(0, 40), needle.slice(0, 24)]
    .map((s) => s.trim())
    .filter((s) => s.length >= 8);
  for (const candidate of candidates) {
    const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode();
    while (node) {
      if (node.nodeValue && node.nodeValue.includes(candidate)) {
        return node;
      }
      node = walker.nextNode();
    }
  }
  return null;
}
