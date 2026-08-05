const CONFIG_ENDPOINT = "/api/config";
const RECENT_LIMIT = 5;
const ALERT_RULE_LIMIT = 8;
const ALERT_RUN_LIMIT = 5;
const WATCHLIST_PREVIEW_LIMIT = 5;
const TIMELINE_LIMIT = 12;

let configPromise = null;
let authPromise = null;
const authListeners = new Set();
const authState = {
  client: null,
  error: null,
  ready: false,
  user: null,
};

export function mountAccountControls({ root }) {
  if (!root) {
    return;
  }

  const account = createAccountControls(root);
  bindAccountEvents(account);
  updateAccountControls(account, authState);
  subscribeAuth((state) => updateAccountControls(account, state));
  initAuth();
}

export function mountResearchLibrary({ insertAfter, getRecord, getTicker, modeFilter, openRecord }) {
  if (!insertAfter) {
    return { root: null, setCanSave() {} };
  }

  const panel = createResearchPanel();
  insertAfter.after(panel.root);

  const state = {
    canSave: false,
    client: null,
    user: null,
  };

  const controls = {
    setCanSave(canSave) {
      state.canSave = canSave;
      updateResearchControls(panel, state);
    },
  };

  setupResearchLibrary(panel, state, { getRecord, getTicker, modeFilter, openRecord });
  return { ...controls, root: panel.root };
}

export function mountAlertMonitor({ insertAfter, getDraft, openRun, runRule }) {
  if (!insertAfter) {
    return { refresh() {} };
  }

  const panel = createAlertMonitorPanel();
  insertAfter.after(panel.root);
  const state = {
    client: null,
    user: null,
  };
  const callbacks = { getDraft, openRun, runRule };
  bindAlertMonitorEvents(panel, state, callbacks);
  subscribeAuth(async (auth) => {
    state.client = auth.client;
    state.user = auth.user;

    if (auth.error) {
      showAlertMonitorStatus(panel, `Alert monitor unavailable: ${auth.error}`);
      panel.root.hidden = false;
      panel.actions.hidden = true;
      panel.list.hidden = true;
      return;
    }

    if (!auth.ready && !auth.client) {
      showAlertMonitorStatus(panel, "Checking alert monitor...");
      return;
    }

    panel.root.hidden = false;
    updateAlertMonitorControls(panel, state);
    if (state.user) {
      await loadAlertMonitor(panel, state, callbacks, true);
    }
  });
  initAuth();
  return {
    root: panel.root,
    refresh() {
      return loadAlertMonitor(panel, state, callbacks, false);
    },
  };
}

export function mountSavedWatchlistCockpit({ root, getDraft, applyWatchlist, runCockpit, runAlerts }) {
  if (!root) {
    return { refresh() {} };
  }

  const panel = createSavedWatchlistPanel(root);
  const state = {
    client: null,
    user: null,
  };
  const callbacks = { getDraft, applyWatchlist, runCockpit, runAlerts };
  bindSavedWatchlistEvents(panel, state, callbacks);
  subscribeAuth(async (auth) => {
    state.client = auth.client;
    state.user = auth.user;

    if (auth.error) {
      showSavedWatchlistStatus(panel, `Watchlists unavailable: ${auth.error}`);
      panel.actions.hidden = true;
      panel.list.hidden = true;
      panel.save.disabled = true;
      panel.refresh.disabled = true;
      return;
    }

    if (!auth.ready && !auth.client) {
      showSavedWatchlistStatus(panel, "Checking saved watchlists...");
      return;
    }

    updateSavedWatchlistControls(panel, state);
    if (state.user) {
      await loadSavedWatchlists(panel, state, callbacks, true);
    }
  });
  initAuth();
  return {
    refresh() {
      return loadSavedWatchlists(panel, state, callbacks, false);
    },
  };
}

async function setupResearchLibrary(panel, state, callbacks) {
  bindResearchEvents(panel, state, callbacks);
  subscribeAuth(async (auth) => {
    state.client = auth.client;
    state.user = auth.user;

    if (auth.error) {
      showResearchStatus(panel, `Library unavailable: ${auth.error}`);
      panel.root.hidden = false;
      return;
    }

    if (!auth.ready && !auth.client) {
      showResearchStatus(panel, "Checking saved research...");
      return;
    }

    panel.root.hidden = false;
    updateResearchControls(panel, state);
    if (state.user) {
      await loadRecentResearch(panel, state, callbacks, true);
    }
  });
  await initAuth();
}

function bindResearchEvents(panel, state, callbacks) {
  panel.email.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      signInFromPanel(panel, state);
    }
  });
  panel.signIn.addEventListener("click", () => signInFromPanel(panel, state));
  panel.signOut.addEventListener("click", () => signOutSession(panel, state));
  panel.save.addEventListener("click", () => saveResearch(panel, state, callbacks));
  panel.recent.addEventListener("click", () => loadRecentResearch(panel, state, callbacks, false));
  panel.timeline.addEventListener("click", () => loadTickerTimeline(panel, state, callbacks, false));
  panel.timelineTicker.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      loadTickerTimeline(panel, state, callbacks, false);
    }
  });
}

function bindSavedWatchlistEvents(panel, state, callbacks) {
  panel.save.addEventListener("click", () => saveSavedWatchlist(panel, state, callbacks));
  panel.refresh.addEventListener("click", () => loadSavedWatchlists(panel, state, callbacks, false));
  panel.name.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveSavedWatchlist(panel, state, callbacks);
    }
  });
}

function bindAlertMonitorEvents(panel, state, callbacks) {
  panel.save.addEventListener("click", () => saveAlertRule(panel, state, callbacks));
  panel.refresh.addEventListener("click", () => loadAlertMonitor(panel, state, callbacks, false));
  panel.webhookEnabled.addEventListener("change", () => updateAlertDeliveryFields(panel));
  panel.name.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      saveAlertRule(panel, state, callbacks);
    }
  });
}

function bindAccountEvents(account) {
  account.toggle.addEventListener("click", () => toggleAccountPanel(account));
  account.email.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      signInFromAccount(account);
    }
  });
  account.signIn.addEventListener("click", () => signInFromAccount(account));
  account.signOut.addEventListener("click", () => signOutSession(account, authState));
  document.addEventListener("click", (event) => {
    if (!account.root.contains(event.target)) {
      closeAccountPanel(account);
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeAccountPanel(account);
    }
  });
}

async function signInFromPanel(panel, state) {
  const email = panel.email.value.trim();
  if (!email) {
    showResearchStatus(panel, "Enter an email for a Supabase magic link.");
    panel.email.focus();
    return;
  }

  showResearchStatus(panel, "Sending magic link...");
  panel.signIn.disabled = true;
  const error = await sendMagicLink(state.client, email);
  panel.signIn.disabled = false;

  if (error) {
    showResearchStatus(panel, error.message);
    return;
  }
  showResearchStatus(panel, "Check your email, then return here signed in.");
}

async function signInFromAccount(account) {
  const email = account.email.value.trim();
  if (!email) {
    showAccountStatus(account, "Enter an email for a magic link.");
    account.email.focus();
    return;
  }
  if (!authState.client) {
    showAccountStatus(account, authState.error || "Auth is not ready yet.");
    return;
  }

  account.signIn.disabled = true;
  showAccountStatus(account, "Sending magic link...");
  const error = await sendMagicLink(authState.client, email);
  account.signIn.disabled = false;

  if (error) {
    showAccountStatus(account, error.message);
    return;
  }
  showAccountStatus(account, "Check your email, then return here signed in.");
}

async function sendMagicLink(client, email) {
  if (!client) {
    return new Error("Auth is not ready yet.");
  }
  const { error } = await client.auth.signInWithOtp({
    email,
    options: {
      emailRedirectTo: window.location.href.split("#")[0],
    },
  });
  return error;
}

async function signOutSession(panel, state) {
  if (!state.client) {
    return;
  }
  await state.client.auth.signOut();
  state.user = null;
  if (state.client === authState.client) {
    authState.user = null;
  }
  if (panel.list) {
    panel.list.hidden = true;
    panel.list.innerHTML = "";
  }
  closeAccountPanel(panel);
  showAccountStatus(panel, "Signed out.");
  notifyAuth();
}

async function saveResearch(panel, state, callbacks) {
  if (!state.user) {
    showResearchStatus(panel, "Sign in before saving research.");
    return;
  }
  if (!state.canSave) {
    showResearchStatus(panel, "Generate an output first, then save it.");
    return;
  }

  const record = callbacks.getRecord();
  if (!record?.payload) {
    showResearchStatus(panel, "No research payload is ready to save.");
    return;
  }

  panel.save.disabled = true;
  showResearchStatus(panel, "Saving research...");
  const { error } = await state.client
    .from("research_runs")
    .insert({
      user_id: state.user.id,
      mode: record.mode || "research",
      ticker: record.ticker || null,
      title: record.title || "Saved research",
      summary: record.summary || null,
      source_url: record.source_url || null,
      payload: record.payload,
    })
    .select("id")
    .single();
  panel.save.disabled = false;

  if (error) {
    showResearchStatus(panel, error.message);
    return;
  }
  showResearchStatus(panel, "Saved to your research library.");
  await loadRecentResearch(panel, state, callbacks, true);
}

async function loadRecentResearch(panel, state, callbacks, quiet) {
  if (!state.user) {
    showResearchStatus(panel, "Sign in to load saved research.");
    return;
  }

  if (!quiet) {
    showResearchStatus(panel, "Loading recent research...");
  }

  let query = state.client
    .from("research_runs")
    .select("id,title,mode,ticker,summary,created_at,payload")
    .order("created_at", { ascending: false })
    .limit(RECENT_LIMIT);
  if (callbacks.modeFilter) {
    query = query.eq("mode", callbacks.modeFilter);
  }
  const { data, error } = await query;

  if (error) {
    showResearchStatus(panel, error.message);
    return;
  }

  renderRecentResearch(panel, data || [], callbacks);
  if (!quiet) {
    showResearchStatus(panel, data?.length ? "Recent research loaded." : "No saved research yet.");
  }
}

async function loadTickerTimeline(panel, state, callbacks, quiet) {
  if (!state.user) {
    showResearchStatus(panel, "Sign in to load a ticker timeline.");
    return;
  }

  const ticker = timelineTicker(panel, callbacks);
  if (!ticker) {
    showResearchStatus(panel, "Enter a ticker for the timeline.");
    panel.timelineTicker.focus();
    return;
  }

  panel.timelineTicker.value = ticker;
  if (!quiet) {
    showResearchStatus(panel, `Loading ${ticker} timeline...`);
  }

  const { data, error } = await state.client
    .from("research_runs")
    .select("id,title,mode,ticker,summary,created_at,payload")
    .ilike("ticker", ticker)
    .order("created_at", { ascending: false })
    .limit(TIMELINE_LIMIT);

  if (error) {
    showResearchStatus(panel, error.message);
    return;
  }

  renderTickerTimeline(panel, data || [], callbacks, ticker);
  if (!quiet) {
    showResearchStatus(panel, data?.length ? `${ticker} timeline loaded.` : `No saved ${ticker} research yet.`);
  }
}

function timelineTicker(panel, callbacks) {
  const typed = panel.timelineTicker.value.trim().toUpperCase();
  if (typed) {
    return typed;
  }
  const record = callbacks.getRecord?.();
  return normalizeTicker(record?.ticker || callbacks.getTicker?.());
}

async function saveSavedWatchlist(panel, state, callbacks) {
  if (!state.user) {
    showSavedWatchlistStatus(panel, "Sign in before saving watchlists.");
    return;
  }

  const draft = callbacks.getDraft();
  const sourceUrl = String(draft.source_url || "").trim();
  let tickers = normalizeTickerList(draft.tickers);
  let name = panel.name.value.trim();
  const metadata = {
    max_results: Number(draft.max_results || 10),
    saved_from: "terminal",
  };

  panel.save.disabled = true;
  try {
    if (sourceUrl) {
      showSavedWatchlistStatus(panel, "Resolving watchlist...");
      const resolved = await resolveWatchlistSource(sourceUrl, metadata.max_results);
      tickers = normalizeTickerList(resolved.tickers);
      metadata.watchlist = resolved.watchlist;
      name ||= resolved.watchlist?.name || "TradingView watchlist";
    }

    if (!tickers.length) {
      showSavedWatchlistStatus(panel, "Add tickers or a public TradingView watchlist first.");
      return;
    }

    const { error } = await state.client.from("saved_watchlists").insert({
      user_id: state.user.id,
      name: name || "Saved watchlist",
      source_url: sourceUrl || null,
      tickers,
      metadata,
    });

    if (error) {
      showSavedWatchlistStatus(panel, error.message);
      return;
    }

    panel.name.value = "";
    showSavedWatchlistStatus(panel, "Saved watchlist.");
    await loadSavedWatchlists(panel, state, callbacks, true);
  } catch (error) {
    showSavedWatchlistStatus(panel, error.message || "Could not save watchlist.");
  } finally {
    panel.save.disabled = false;
  }
}

async function loadSavedWatchlists(panel, state, callbacks, quiet) {
  if (!state.user) {
    showSavedWatchlistStatus(panel, "Sign in to load saved watchlists.");
    return;
  }

  if (!quiet) {
    showSavedWatchlistStatus(panel, "Loading saved watchlists...");
  }

  const { data, error } = await state.client
    .from("saved_watchlists")
    .select("id,name,source_url,tickers,metadata,created_at,updated_at")
    .order("created_at", { ascending: false });

  if (error) {
    showSavedWatchlistStatus(panel, error.message);
    return;
  }

  renderSavedWatchlists(panel, data || [], callbacks, state);
  if (!quiet) {
    showSavedWatchlistStatus(panel, data?.length ? "Saved watchlists loaded." : "No saved watchlists yet.");
  }
}

async function deleteSavedWatchlist(panel, state, callbacks, row) {
  if (!row.id) {
    return;
  }
  showSavedWatchlistStatus(panel, "Removing watchlist...");
  const { error } = await state.client.from("saved_watchlists").delete().eq("id", row.id);
  if (error) {
    showSavedWatchlistStatus(panel, error.message);
    return;
  }
  await loadSavedWatchlists(panel, state, callbacks, true);
  showSavedWatchlistStatus(panel, "Removed watchlist.");
}

async function saveAlertRule(panel, state, callbacks) {
  if (!state.user) {
    showAlertMonitorStatus(panel, "Sign in before saving alert rules.");
    return;
  }

  const draft = callbacks.getDraft();
  const sourceUrl = String(draft.source_url || "").trim();
  let tickers = normalizeTickerList(draft.tickers);
  const maxResults = clampNumber(draft.max_results, 1, 50, 10);
  const maxAlerts = clampNumber(draft.max_alerts, 1, 50, 12);
  const volatilityThreshold = clampNumber(draft.volatility_threshold, 0, 2, 0.55);
  const webhookEnabled = panel.webhookEnabled.checked;
  const webhookUrl = panel.webhookUrl.value.trim();
  const deliveryMinSeverity = panel.deliveryMinSeverity.value === "high" ? "high" : "any";
  let name = panel.name.value.trim();
  const metadata = {
    saved_from: "alert-monitor",
  };

  panel.save.disabled = true;
  try {
    if (sourceUrl) {
      showAlertMonitorStatus(panel, "Resolving watchlist for alert rule...");
      const resolved = await resolveWatchlistSource(sourceUrl, maxResults);
      tickers = normalizeTickerList(resolved.tickers);
      metadata.watchlist = resolved.watchlist;
      name ||= resolved.watchlist?.name || "Daily alert rule";
    }

    if (!tickers.length) {
      showAlertMonitorStatus(panel, "Add tickers or a public TradingView watchlist first.");
      return;
    }

    if (webhookEnabled && !webhookUrl) {
      showAlertMonitorStatus(panel, "Add a webhook URL or turn webhook delivery off.");
      return;
    }

    const { error } = await state.client.from("alert_rules").insert({
      user_id: state.user.id,
      name: name || "Daily alert rule",
      source_url: sourceUrl || null,
      tickers,
      active: true,
      schedule: "daily",
      period: draft.period || "1y",
      max_results: maxResults,
      max_alerts: maxAlerts,
      volatility_threshold: volatilityThreshold,
      delivery_channel: webhookEnabled ? "webhook" : "none",
      delivery_webhook_url: webhookEnabled ? webhookUrl : null,
      delivery_min_severity: webhookEnabled ? deliveryMinSeverity : "any",
      metadata,
    });

    if (error) {
      showAlertMonitorStatus(panel, error.message);
      return;
    }

    panel.name.value = "";
    panel.webhookEnabled.checked = false;
    panel.webhookUrl.value = "";
    panel.deliveryMinSeverity.value = "any";
    updateAlertDeliveryFields(panel);
    showAlertMonitorStatus(panel, "Saved daily alert rule.");
    await loadAlertMonitor(panel, state, callbacks, true);
  } catch (error) {
    showAlertMonitorStatus(panel, error.message || "Could not save alert rule.");
  } finally {
    panel.save.disabled = false;
  }
}

async function loadAlertMonitor(panel, state, callbacks, quiet) {
  if (!state.user) {
    showAlertMonitorStatus(panel, "Sign in to load alert rules.");
    return;
  }

  if (!quiet) {
    showAlertMonitorStatus(panel, "Loading alert monitor...");
  }

  const [rulesResult, runsResult, deliveriesResult] = await Promise.all([
    state.client
      .from("alert_rules")
      .select(
        "id,name,source_url,tickers,active,schedule,period,max_results,max_alerts,volatility_threshold,delivery_channel,delivery_webhook_url,delivery_min_severity,last_run_at,last_run_date,metadata,created_at,updated_at",
      )
      .order("updated_at", { ascending: false })
      .limit(ALERT_RULE_LIMIT),
    state.client
      .from("alert_runs")
      .select(
        "id,alert_rule_id,trigger,status,run_date,alert_count,high_alert_count,digest,alerts,rows,payload,error,delivery_status,delivery_channel,delivered_at,created_at",
      )
      .order("created_at", { ascending: false })
      .limit(ALERT_RUN_LIMIT),
    state.client
      .from("alert_deliveries")
      .select(
        "id,alert_run_id,channel,status,destination,response_status,error,payload,created_at",
      )
      .order("created_at", { ascending: false })
      .limit(ALERT_RUN_LIMIT * 3),
  ]);

  if (rulesResult.error) {
    showAlertMonitorStatus(panel, rulesResult.error.message);
    return;
  }
  if (runsResult.error) {
    showAlertMonitorStatus(panel, runsResult.error.message);
    return;
  }
  if (deliveriesResult.error) {
    showAlertMonitorStatus(panel, deliveriesResult.error.message);
    return;
  }

  renderAlertMonitor(
    panel,
    rulesResult.data || [],
    runsResult.data || [],
    deliveriesResult.data || [],
    callbacks,
    state,
  );
  if (!quiet) {
    showAlertMonitorStatus(panel, "Alert monitor loaded.");
  }
}

async function runAlertRule(panel, state, callbacks, row) {
  if (!state.user) {
    showAlertMonitorStatus(panel, "Sign in before running alert rules.");
    return;
  }

  showAlertMonitorStatus(panel, `Running ${row.name || "alert rule"}...`);
  try {
    const payload = await callbacks.runRule(row);
    const meta = payload.meta || {};
    const now = new Date();
    const runDate = now.toISOString().slice(0, 10);
    const { error } = await state.client.from("alert_runs").insert({
      alert_rule_id: row.id,
      user_id: state.user.id,
      trigger: "manual",
      status: "success",
      run_date: runDate,
      alert_count: Number(meta.alert_count || 0),
      high_alert_count: Number(meta.high_alert_count || 0),
      digest: payload.digest || {},
      alerts: payload.alerts || [],
      rows: payload.rows || [],
      payload,
    });
    if (error) {
      showAlertMonitorStatus(panel, error.message);
      return;
    }
    await state.client
      .from("alert_rules")
      .update({
        last_run_at: now.toISOString(),
        last_run_date: runDate,
        updated_at: now.toISOString(),
      })
      .eq("id", row.id);
    await loadAlertMonitor(panel, state, callbacks, true);
    showAlertMonitorStatus(panel, "Alert run saved to inbox.");
  } catch (error) {
    showAlertMonitorStatus(panel, error.message || "Could not run alert rule.");
  }
}

async function testAlertWebhook(panel, state, callbacks, row, button) {
  if (!state.user) {
    showAlertMonitorStatus(panel, "Sign in before testing webhook delivery.");
    return;
  }
  if (row.delivery_channel !== "webhook" || !row.delivery_webhook_url) {
    showAlertMonitorStatus(panel, "Add webhook delivery to this rule before testing.");
    return;
  }

  button.disabled = true;
  showAlertMonitorStatus(panel, `Testing webhook for ${row.name || "alert rule"}...`);
  try {
    const { data: sessionData, error: sessionError } = await state.client.auth.getSession();
    const token = sessionData?.session?.access_token;
    if (sessionError || !token) {
      throw new Error(sessionError?.message || "Sign in again before testing webhook delivery.");
    }
    const response = await fetch("/api/alerts/webhook/test", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        Authorization: `Bearer ${token}`,
      },
      body: JSON.stringify({ rule_id: row.id }),
    });
    const payload = await response.json();
    if (!response.ok) {
      throw new Error(payload.error || "Could not test webhook delivery.");
    }
    await loadAlertMonitor(panel, state, callbacks, true);
    const status = payload.delivery?.status || "sent";
    showAlertMonitorStatus(panel, `Webhook test ${status}.`);
  } catch (error) {
    showAlertMonitorStatus(panel, error.message || "Could not test webhook delivery.");
  } finally {
    button.disabled = false;
  }
}

async function deleteAlertRule(panel, state, callbacks, row) {
  if (!row.id) {
    return;
  }
  showAlertMonitorStatus(panel, "Removing alert rule...");
  const { error } = await state.client.from("alert_rules").delete().eq("id", row.id);
  if (error) {
    showAlertMonitorStatus(panel, error.message);
    return;
  }
  await loadAlertMonitor(panel, state, callbacks, true);
  showAlertMonitorStatus(panel, "Removed alert rule.");
}

async function resolveWatchlistSource(sourceUrl, maxResults) {
  const response = await fetch("/api/watchlists/resolve", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      watchlist_url: sourceUrl,
      max_results: maxResults,
    }),
  });
  const data = await response.json();
  if (!response.ok) {
    throw new Error(data.error || "Could not resolve watchlist");
  }
  return data;
}

function renderRecentResearch(panel, rows, callbacks) {
  panel.list.innerHTML = "";
  panel.list.hidden = false;

  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = "No saved research yet.";
    panel.list.append(empty);
    return;
  }

  rows.forEach((row) => {
    const item = document.createElement("button");
    item.className = "research-row";
    item.type = "button";
    item.addEventListener("click", () => callbacks.openRecord(row));

    const title = document.createElement("strong");
    title.textContent = row.title || "Saved research";
    const meta = document.createElement("span");
    meta.textContent = [row.mode, row.ticker, relativeDate(row.created_at)]
      .filter(Boolean)
      .join(" / ");

    item.append(title, meta);
    panel.list.append(item);
  });
}

function renderTickerTimeline(panel, rows, callbacks, ticker) {
  panel.list.innerHTML = "";
  panel.list.hidden = false;

  const root = document.createElement("section");
  root.className = "ticker-timeline";
  root.append(timelineHeader(rows, ticker));

  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = `No saved ${ticker} research yet.`;
    root.append(empty);
    panel.list.append(root);
    return;
  }

  const latest = rows[0];
  const previous = rows[1];
  const insight = document.createElement("p");
  insight.className = "ticker-timeline-insight";
  insight.textContent = timelineChangeText(latest, previous, ticker);
  root.append(insight);

  const list = document.createElement("div");
  list.className = "ticker-timeline-list";
  rows.forEach((row, index) => list.append(timelineRunCard(row, callbacks, index)));
  root.append(list);
  panel.list.append(root);
}

function timelineHeader(rows, ticker) {
  const header = document.createElement("div");
  header.className = "ticker-timeline-head";
  const title = document.createElement("div");
  title.className = "ticker-timeline-title";
  const strong = document.createElement("strong");
  strong.textContent = `${ticker} Timeline`;
  const span = document.createElement("span");
  span.textContent = rows.length ? `${rows.length} saved runs` : "No saved runs";
  title.append(strong, span);

  const metrics = document.createElement("div");
  metrics.className = "ticker-timeline-metrics";
  const modes = new Set(rows.map((row) => row.mode).filter(Boolean));
  metrics.append(
    previewMetric("Runs", rows.length),
    previewMetric("Modes", modes.size),
    previewMetric("Latest", relativeDate(rows[0]?.created_at) || "N/A"),
  );
  header.append(title, metrics);
  return header;
}

function timelineRunCard(row, callbacks, index) {
  const card = document.createElement("article");
  card.className = "ticker-timeline-run";
  const button = document.createElement("button");
  button.className = "research-row ticker-timeline-open";
  button.type = "button";
  button.addEventListener("click", () => callbacks.openRecord(row));

  const title = document.createElement("strong");
  title.textContent = row.title || `${row.ticker || "Ticker"} research`;
  const meta = document.createElement("span");
  meta.textContent = [index === 0 ? "Latest" : null, row.mode, relativeDate(row.created_at)]
    .filter(Boolean)
    .join(" / ");
  button.append(title, meta);

  const signals = timelineSignals(row);
  const chips = document.createElement("div");
  chips.className = "ticker-timeline-chips";
  [
    ["Lane", signals.lane],
    ["Score", formatCompactValue(signals.score)],
    ["Flow", signals.flow],
    ["Ridge", signals.ridge],
    ["Auction", signals.auction],
    ["Price", formatTimelineCurrency(signals.price)],
  ]
    .filter(([_label, value]) => value && value !== "N/A")
    .forEach(([label, value]) => {
      const chip = document.createElement("span");
      chip.textContent = `${label}: ${value}`;
      chips.append(chip);
    });

  card.append(button);
  if (chips.children.length) {
    card.append(chips);
  }
  return card;
}

function timelineChangeText(latest, previous, ticker) {
  if (!previous) {
    return `First saved run in the ${ticker} notebook.`;
  }

  const latestSignals = timelineSignals(latest);
  const previousSignals = timelineSignals(previous);
  const changes = [];
  appendSignalChange(changes, "Lane", previousSignals.lane, latestSignals.lane);
  appendSignalChange(changes, "Flow", previousSignals.flow, latestSignals.flow);
  appendSignalChange(changes, "Ridge", previousSignals.ridge, latestSignals.ridge);
  appendSignalChange(changes, "Auction", previousSignals.auction, latestSignals.auction);
  appendScoreChange(changes, previousSignals.score, latestSignals.score);
  appendPriceChange(changes, previousSignals.price, latestSignals.price);

  if (!changes.length) {
    return `No headline metric changed since the prior saved ${ticker} run.`;
  }
  return `Changed since prior run: ${changes.join("; ")}.`;
}

function appendSignalChange(changes, label, previous, latest) {
  if (previous && latest && previous !== latest) {
    changes.push(`${label} ${previous} -> ${latest}`);
  }
}

function appendScoreChange(changes, previous, latest) {
  const previousNumber = Number(previous);
  const latestNumber = Number(latest);
  if (!Number.isFinite(previousNumber) || !Number.isFinite(latestNumber)) {
    return;
  }
  const delta = latestNumber - previousNumber;
  if (Math.abs(delta) >= 0.01) {
    changes.push(`Score ${delta >= 0 ? "+" : ""}${formatCompactValue(delta)}`);
  }
}

function appendPriceChange(changes, previous, latest) {
  const previousNumber = Number(previous);
  const latestNumber = Number(latest);
  if (!Number.isFinite(previousNumber) || !Number.isFinite(latestNumber) || previousNumber === 0) {
    return;
  }
  const delta = (latestNumber - previousNumber) / previousNumber;
  if (Math.abs(delta) >= 0.001) {
    changes.push(`Price ${delta >= 0 ? "+" : ""}${formatTimelinePercent(delta)}`);
  }
}

function timelineSignals(row) {
  const payload = row.payload || {};
  const ticker = normalizeTicker(
    row.ticker || payload.ticker || payload.Ticker || payload.meta?.ticker || payload.tickers?.[0],
  );
  const cockpitRow = Array.isArray(payload.rows)
    ? payload.rows.find((candidate) => normalizeTicker(candidate.ticker) === ticker)
    : null;
  const summary = payload.summary || cockpitRow?.summary || {};
  const ridge = payload.ridge || cockpitRow?.ridge || {};
  const flow = payload.flow || payload.flow_compass || cockpitRow?.flow || {};
  const auction = payload.auction || cockpitRow?.auction || {};

  return {
    lane: firstValue(cockpitRow?.lane, payload.lane, payload.meta?.lane),
    score: firstNumber(cockpitRow?.score, payload.score, payload.meta?.score),
    flow: firstValue(flow.state, flow.signal),
    ridge: firstValue(ridge.recommendation, ridge.state),
    auction: firstValue(auction.location),
    price: firstNumber(cockpitRow?.price, summary.price, payload.price, payload.meta?.price),
  };
}

function renderSavedWatchlists(panel, rows, callbacks, state) {
  panel.list.innerHTML = "";
  panel.list.hidden = false;

  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = "No saved watchlists yet.";
    panel.list.append(empty);
    return;
  }

  rows.forEach((row) => {
    panel.list.append(savedWatchlistCard(panel, state, callbacks, row));
  });
}

function savedWatchlistCard(panel, state, callbacks, row) {
  const item = document.createElement("article");
  item.className = "saved-watchlist-card";

  const head = document.createElement("div");
  head.className = "saved-watchlist-card-head";

  const body = document.createElement("div");
  body.className = "saved-watchlist-body";
  const title = document.createElement("strong");
  title.textContent = row.name || "Saved watchlist";
  const meta = document.createElement("span");
  meta.textContent = [
    `${normalizeTickerList(row.tickers).length} tickers`,
    row.source_url ? "TradingView" : "Manual",
    relativeDate(row.updated_at || row.created_at),
  ]
    .filter(Boolean)
    .join(" / ");
  body.append(title, meta);

  const actions = document.createElement("div");
  actions.className = "saved-watchlist-row-actions";

  const load = document.createElement("button");
  load.className = "download-link";
  load.type = "button";
  load.textContent = "Load";
  load.addEventListener("click", () => {
    callbacks.applyWatchlist(row);
    showSavedWatchlistStatus(panel, `Loaded ${row.name || "watchlist"}.`);
  });

  const rank = document.createElement("button");
  rank.className = "export-button";
  rank.type = "button";
  rank.textContent = "Rank";

  const cockpit = document.createElement("button");
  cockpit.className = "download-link";
  cockpit.type = "button";
  cockpit.textContent = "Cockpit";
  cockpit.disabled = !callbacks.runCockpit;
  cockpit.addEventListener("click", () =>
    runSavedWatchlistAction(panel, row, callbacks.runCockpit, "Opening cockpit...", "Cockpit loaded."),
  );

  const alerts = document.createElement("button");
  alerts.className = "download-link";
  alerts.type = "button";
  alerts.textContent = "Alerts";
  alerts.disabled = !callbacks.runAlerts;
  alerts.addEventListener("click", () =>
    runSavedWatchlistAction(panel, row, callbacks.runAlerts, "Opening alerts...", "Alert digest loaded."),
  );

  const remove = document.createElement("button");
  remove.className = "download-link saved-watchlist-delete";
  remove.type = "button";
  remove.textContent = "Delete";
  remove.addEventListener("click", () => deleteSavedWatchlist(panel, state, callbacks, row));

  actions.append(load, rank, cockpit, alerts, remove);
  head.append(body, actions);

  const preview = document.createElement("div");
  preview.className = "saved-watchlist-preview";
  preview.hidden = true;
  rank.addEventListener("click", () => previewSavedWatchlistCockpit(panel, row, preview, rank));

  item.append(head, preview);
  return item;
}

async function runSavedWatchlistAction(panel, row, callback, loadingMessage, doneMessage) {
  if (!callback) {
    return;
  }
  showSavedWatchlistStatus(panel, loadingMessage);
  try {
    await callback(row);
    showSavedWatchlistStatus(panel, doneMessage);
  } catch (error) {
    showSavedWatchlistStatus(panel, error.message || "Could not run saved watchlist.");
  }
}

async function previewSavedWatchlistCockpit(panel, row, preview, button) {
  button.disabled = true;
  preview.hidden = false;
  preview.innerHTML = "";
  const loading = document.createElement("p");
  loading.className = "research-empty";
  loading.textContent = "Ranking watchlist...";
  preview.append(loading);
  showSavedWatchlistStatus(panel, `Ranking ${row.name || "watchlist"}...`);

  try {
    const response = await fetch("/api/watchlists/cockpit", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(savedWatchlistCockpitPayload(row)),
    });
    const data = await response.json();
    if (!response.ok) {
      throw new Error(data.error || "Could not rank watchlist");
    }
    renderSavedWatchlistPreview(preview, data);
    const rows = data.rows || [];
    showSavedWatchlistStatus(
      panel,
      rows.length ? `Ranked ${rows.length} names from ${row.name || "watchlist"}.` : "No cockpit rows returned.",
    );
  } catch (error) {
    preview.innerHTML = "";
    const message = document.createElement("p");
    message.className = "research-empty";
    message.textContent = error.message || "Could not rank watchlist.";
    preview.append(message);
    showSavedWatchlistStatus(panel, message.textContent);
  } finally {
    button.disabled = false;
  }
}

function savedWatchlistCockpitPayload(row) {
  const tickers = normalizeTickerList(row.tickers);
  return {
    ticker: tickers[0] || "AAPL",
    tickers,
    watchlist_url: row.source_url || "",
    max_results: clampNumber(row.max_results || row.metadata?.max_results, 1, 50, 10),
    period: row.period || row.metadata?.period || "1y",
  };
}

function renderSavedWatchlistPreview(root, data) {
  const rows = data.rows || [];
  root.innerHTML = "";

  const summary = document.createElement("div");
  summary.className = "saved-watchlist-cockpit-summary";
  const priorityCount = rows.filter((row) => row.lane === "Priority").length;
  const riskCount = rows.filter((row) => row.lane === "Risk").length;
  const averageScore = rows.length
    ? rows.reduce((total, row) => total + Number(row.score || 0), 0) / rows.length
    : 0;
  summary.append(
    previewMetric("Top", rows[0]?.ticker || "N/A"),
    previewMetric("Priority", priorityCount),
    previewMetric("Risk", riskCount),
    previewMetric("Avg", formatCompactValue(averageScore)),
  );
  if (data.meta?.error_count) {
    summary.append(previewMetric("Skipped", data.meta.error_count));
  }
  root.append(summary);

  if (!rows.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = "No cockpit rows returned.";
    root.append(empty);
    return;
  }

  const tableWrap = document.createElement("div");
  tableWrap.className = "saved-watchlist-queue-wrap";
  const table = document.createElement("table");
  table.className = "saved-watchlist-queue-table";
  table.innerHTML = `
    <thead>
      <tr>
        <th>Rank</th>
        <th>Ticker</th>
        <th>Lane</th>
        <th>Score</th>
        <th>Flow</th>
        <th>Ridge</th>
        <th>Auction</th>
      </tr>
    </thead>
  `;
  const body = document.createElement("tbody");
  rows.slice(0, WATCHLIST_PREVIEW_LIMIT).forEach((row) => body.append(savedWatchlistQueueRow(row)));
  table.append(body);
  tableWrap.append(table);
  root.append(tableWrap);

  const foot = document.createElement("p");
  foot.className = "saved-watchlist-preview-foot";
  foot.textContent = `Showing ${Math.min(rows.length, WATCHLIST_PREVIEW_LIMIT)} of ${rows.length}`;
  if (data.meta?.period) {
    foot.textContent += ` / ${String(data.meta.period).toUpperCase()}`;
  }
  root.append(foot);
}

function savedWatchlistQueueRow(row) {
  const tr = document.createElement("tr");
  tr.dataset.lane = String(row.lane || "Review").toLowerCase();
  const values = [
    row.rank,
    row.ticker,
    row.lane,
    formatCompactValue(row.score),
    compactFlowLabel(row),
    row.ridge?.recommendation || "N/A",
    row.auction?.location || "N/A",
  ];
  values.forEach((value, index) => {
    const cell = document.createElement(index === 1 ? "th" : "td");
    if (index === 1) {
      cell.scope = "row";
    }
    if (index === 2) {
      const pill = document.createElement("span");
      pill.className = "saved-watchlist-lane";
      pill.textContent = value || "Review";
      cell.append(pill);
    } else {
      cell.textContent = value;
    }
    tr.append(cell);
  });
  return tr;
}

function previewMetric(label, value) {
  const metric = document.createElement("div");
  metric.className = "saved-watchlist-preview-metric";
  const strong = document.createElement("strong");
  strong.textContent = value;
  const span = document.createElement("span");
  span.textContent = label;
  metric.append(strong, span);
  return metric;
}

function compactFlowLabel(row) {
  const state = row.flow?.state || "N/A";
  const score = row.flow?.score;
  return Number.isFinite(Number(score)) ? `${state} ${formatCompactValue(Number(score))}` : state;
}

function formatCompactValue(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return String(value || "N/A");
  }
  if (Number.isInteger(number)) {
    return String(number);
  }
  return Math.abs(number) >= 100 ? number.toFixed(0) : number.toFixed(2);
}

function renderAlertMonitor(panel, rules, runs, deliveries, callbacks, state) {
  panel.list.innerHTML = "";
  panel.list.hidden = false;
  const deliveriesByRun = deliveryMap(deliveries);

  const rulesSection = document.createElement("div");
  rulesSection.className = "alert-monitor-section";
  rulesSection.append(sectionKicker("Daily Rules"));
  if (!rules.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = "No alert rules yet.";
    rulesSection.append(empty);
  } else {
    rules.forEach((row) => rulesSection.append(alertRuleRow(panel, state, callbacks, row)));
  }

  const runsSection = document.createElement("div");
  runsSection.className = "alert-monitor-section";
  runsSection.append(sectionKicker("Inbox"));
  if (!runs.length) {
    const empty = document.createElement("p");
    empty.className = "research-empty";
    empty.textContent = "No alert runs yet.";
    runsSection.append(empty);
  } else {
    runs.forEach((row) => runsSection.append(alertRunRow(callbacks, row, deliveriesByRun.get(row.id))));
  }

  panel.list.append(rulesSection, runsSection);
}

function alertRuleRow(panel, state, callbacks, row) {
  const item = document.createElement("div");
  item.className = "alert-monitor-row";

  const body = document.createElement("div");
  body.className = "alert-monitor-body";
  const title = document.createElement("strong");
  title.textContent = row.name || "Daily alert rule";
  const meta = document.createElement("span");
  meta.textContent = [
    "Daily",
    `${normalizeTickerList(row.tickers).length} tickers`,
    `${row.max_alerts || 12} max alerts`,
    deliveryRuleLabel(row),
    row.last_run_date ? `last ${relativeDate(row.last_run_date)}` : "not run",
  ].join(" / ");
  body.append(title, meta);

  const actions = document.createElement("div");
  actions.className = "alert-monitor-row-actions";
  const run = document.createElement("button");
  run.className = "export-button";
  run.type = "button";
  run.textContent = "Run";
  run.addEventListener("click", () => runAlertRule(panel, state, callbacks, row));
  const test = document.createElement("button");
  test.className = "download-link";
  test.type = "button";
  test.textContent = "Test";
  test.disabled = row.delivery_channel !== "webhook" || !row.delivery_webhook_url;
  test.addEventListener("click", () => testAlertWebhook(panel, state, callbacks, row, test));
  const remove = document.createElement("button");
  remove.className = "download-link saved-watchlist-delete";
  remove.type = "button";
  remove.textContent = "Delete";
  remove.addEventListener("click", () => deleteAlertRule(panel, state, callbacks, row));
  actions.append(run, test, remove);

  item.append(body, actions);
  return item;
}

function alertRunRow(callbacks, row, delivery) {
  const item = document.createElement("div");
  item.className = "alert-run-card";
  const button = document.createElement("button");
  button.className = "research-row alert-run-row";
  button.type = "button";
  button.addEventListener("click", () => callbacks.openRun({ ...row, delivery }));

  const title = document.createElement("strong");
  const headline = row.digest?.headline || row.error || "Alert run";
  title.textContent = headline;
  const meta = document.createElement("span");
  meta.textContent = [
    row.status || "success",
    row.trigger || "manual",
    `${row.high_alert_count || 0} high`,
    deliveryRunLabel(row, delivery),
    relativeDate(row.created_at || row.run_date),
  ].join(" / ");

  button.append(title, meta);
  item.append(button);
  const detail = deliveryDetail(row, delivery);
  if (detail) {
    item.append(detail);
  }
  return item;
}

function deliveryMap(deliveries) {
  const map = new Map();
  deliveries.forEach((delivery) => {
    if (delivery.alert_run_id && !map.has(delivery.alert_run_id)) {
      map.set(delivery.alert_run_id, delivery);
    }
  });
  return map;
}

function deliveryRuleLabel(row) {
  if (row.delivery_channel !== "webhook") {
    return "no delivery";
  }
  return row.delivery_min_severity === "high" ? "webhook high only" : "webhook on alerts";
}

function deliveryRunLabel(row, delivery) {
  const status = delivery?.status || row.delivery_status;
  if (!status || status === "none") {
    return "no delivery";
  }
  const channel = delivery?.channel || row.delivery_channel || "delivery";
  return `${channel} ${status}`;
}

function deliveryDetail(row, delivery) {
  const status = delivery?.status || row.delivery_status;
  if (!status || status === "none") {
    return null;
  }
  const detail = document.createElement("dl");
  detail.className = "alert-delivery-detail";
  appendDeliveryMetric(detail, "Status", status);
  appendDeliveryMetric(detail, "Destination", delivery?.destination || "n/a");
  appendDeliveryMetric(
    detail,
    "HTTP",
    delivery?.response_status ? String(delivery.response_status) : "n/a",
  );
  appendDeliveryMetric(detail, "Time", relativeDate(delivery?.created_at || row.delivered_at));
  if (delivery?.error) {
    appendDeliveryMetric(detail, "Error", delivery.error);
  }
  return detail;
}

function appendDeliveryMetric(root, label, value) {
  const term = document.createElement("dt");
  term.textContent = label;
  const description = document.createElement("dd");
  description.textContent = value || "n/a";
  root.append(term, description);
}

function sectionKicker(label) {
  const kicker = document.createElement("div");
  kicker.className = "alert-monitor-kicker";
  kicker.textContent = label;
  return kicker;
}

function createResearchPanel() {
  const root = document.createElement("details");
  root.className = "research-panel";
  root.hidden = true;
  root.innerHTML = `
    <summary class="utility-summary">
      <span class="panel-label">Library</span>
      <span class="utility-summary-note">Save and reopen runs</span>
    </summary>
    <div class="utility-body">
    <div class="research-head">
      <p class="research-status" data-role="status">Sign in to save generated research.</p>
      <button class="download-link research-signout" data-role="signout" type="button" hidden>Sign out</button>
    </div>
    <div class="research-auth" data-role="auth">
      <input data-role="email" type="email" autocomplete="email" placeholder="you@example.com" />
      <button class="export-button" data-role="signin" type="button">Send Link</button>
    </div>
    <div class="research-actions" data-role="actions" hidden>
      <button class="export-button" data-role="save" type="button" disabled>Save Research</button>
      <button class="download-link" data-role="recent" type="button">Recent</button>
    </div>
    <div class="research-timeline-controls" data-role="timeline-controls" hidden>
      <input data-role="timeline-ticker" autocomplete="off" placeholder="Ticker timeline" />
      <button class="download-link" data-role="timeline" type="button">Timeline</button>
    </div>
    <div class="research-list" data-role="list" hidden></div>
    </div>
  `;

  return {
    root,
    actions: root.querySelector('[data-role="actions"]'),
    auth: root.querySelector('[data-role="auth"]'),
    email: root.querySelector('[data-role="email"]'),
    list: root.querySelector('[data-role="list"]'),
    recent: root.querySelector('[data-role="recent"]'),
    save: root.querySelector('[data-role="save"]'),
    signIn: root.querySelector('[data-role="signin"]'),
    signOut: root.querySelector('[data-role="signout"]'),
    status: root.querySelector('[data-role="status"]'),
    timeline: root.querySelector('[data-role="timeline"]'),
    timelineControls: root.querySelector('[data-role="timeline-controls"]'),
    timelineTicker: root.querySelector('[data-role="timeline-ticker"]'),
  };
}

function createSavedWatchlistPanel(root) {
  root.className = "saved-watchlist-cockpit";
  root.innerHTML = `
    <div class="research-head">
      <div>
        <div class="panel-label">Saved Watchlists</div>
        <p class="research-status" data-role="status">Sign in to save watchlists.</p>
      </div>
    </div>
    <div class="saved-watchlist-actions" data-role="actions" hidden>
      <input data-role="name" autocomplete="off" placeholder="Watchlist name" />
      <button class="export-button" data-role="save" type="button">Save</button>
      <button class="download-link" data-role="refresh" type="button">Refresh</button>
    </div>
    <div class="research-list saved-watchlist-list" data-role="list" hidden></div>
  `;

  return {
    root,
    actions: root.querySelector('[data-role="actions"]'),
    list: root.querySelector('[data-role="list"]'),
    name: root.querySelector('[data-role="name"]'),
    refresh: root.querySelector('[data-role="refresh"]'),
    save: root.querySelector('[data-role="save"]'),
    status: root.querySelector('[data-role="status"]'),
  };
}

function createAlertMonitorPanel() {
  const root = document.createElement("details");
  root.className = "research-panel alert-monitor-panel";
  root.hidden = true;
  root.innerHTML = `
    <summary class="utility-summary">
      <span class="panel-label">Alert Monitor</span>
      <span class="utility-summary-note">Rules and inbox</span>
    </summary>
    <div class="utility-body">
    <div class="research-head">
      <p class="research-status" data-role="status">Sign in to save daily alert rules.</p>
    </div>
    <div class="alert-monitor-actions" data-role="actions" hidden>
      <input data-role="name" autocomplete="off" placeholder="Daily alert name" />
      <button class="export-button" data-role="save" type="button">Save Rule</button>
      <button class="download-link" data-role="refresh" type="button">Inbox</button>
    </div>
    <div class="alert-delivery-fields" data-role="delivery" hidden>
      <label class="alert-delivery-toggle">
        <input data-role="webhook-enabled" type="checkbox" />
        <span>Webhook</span>
      </label>
      <input data-role="webhook-url" type="url" autocomplete="off" placeholder="https://hooks.example.com/..." disabled />
      <select data-role="delivery-min-severity" aria-label="Send when">
        <option value="any">Any alerts</option>
        <option value="high">High only</option>
      </select>
    </div>
    <div class="research-list alert-monitor-list" data-role="list" hidden></div>
    </div>
  `;

  const panel = {
    root,
    actions: root.querySelector('[data-role="actions"]'),
    delivery: root.querySelector('[data-role="delivery"]'),
    deliveryMinSeverity: root.querySelector('[data-role="delivery-min-severity"]'),
    list: root.querySelector('[data-role="list"]'),
    name: root.querySelector('[data-role="name"]'),
    refresh: root.querySelector('[data-role="refresh"]'),
    save: root.querySelector('[data-role="save"]'),
    status: root.querySelector('[data-role="status"]'),
    webhookEnabled: root.querySelector('[data-role="webhook-enabled"]'),
    webhookUrl: root.querySelector('[data-role="webhook-url"]'),
  };
  updateAlertDeliveryFields(panel);
  return panel;
}

function createAccountControls(root) {
  root.setAttribute("data-account-state", "loading");
  root.innerHTML = `
    <button
      class="account-trigger"
      data-role="toggle"
      type="button"
      aria-expanded="false"
      aria-controls="account-panel"
    >
      <span class="account-led" aria-hidden="true"></span>
      <span>
        <span class="account-kicker">Account</span>
        <strong data-role="label">Checking</strong>
      </span>
    </button>
    <div class="account-popover" data-role="panel" id="account-panel" hidden>
      <p class="account-status" data-role="status">Checking account...</p>
      <div class="account-auth" data-role="auth">
        <input data-role="email" type="email" autocomplete="email" placeholder="you@example.com" aria-label="Email" />
        <button class="export-button" data-role="signin" type="button">Send Link</button>
      </div>
      <div class="account-session" data-role="session" hidden>
        <span data-role="email-label"></span>
        <button class="download-link" data-role="signout" type="button">Log out</button>
      </div>
    </div>
  `;

  return {
    root,
    auth: root.querySelector('[data-role="auth"]'),
    email: root.querySelector('[data-role="email"]'),
    emailLabel: root.querySelector('[data-role="email-label"]'),
    label: root.querySelector('[data-role="label"]'),
    panel: root.querySelector('[data-role="panel"]'),
    session: root.querySelector('[data-role="session"]'),
    signIn: root.querySelector('[data-role="signin"]'),
    signOut: root.querySelector('[data-role="signout"]'),
    status: root.querySelector('[data-role="status"]'),
    toggle: root.querySelector('[data-role="toggle"]'),
  };
}

function updateResearchControls(panel, state) {
  if (state.user) {
    panel.auth.hidden = true;
    panel.actions.hidden = false;
    panel.timelineControls.hidden = false;
    panel.signOut.hidden = false;
    panel.save.disabled = !state.canSave;
    panel.timeline.disabled = false;
    showResearchStatus(panel, `Signed in as ${state.user.email || "Supabase user"}.`);
    return;
  }

  panel.auth.hidden = false;
  panel.actions.hidden = true;
  panel.timelineControls.hidden = true;
  panel.signOut.hidden = true;
  panel.save.disabled = true;
  panel.timeline.disabled = true;
  showResearchStatus(panel, "Sign in to save generated research.");
}

function updateSavedWatchlistControls(panel, state) {
  if (state.user) {
    panel.actions.hidden = false;
    panel.save.disabled = false;
    panel.refresh.disabled = false;
    showSavedWatchlistStatus(panel, "Save or load watchlists for cockpit runs.");
    return;
  }

  panel.actions.hidden = true;
  panel.list.hidden = true;
  panel.save.disabled = true;
  panel.refresh.disabled = true;
  showSavedWatchlistStatus(panel, "Sign in to save watchlists.");
}

function updateAlertMonitorControls(panel, state) {
  if (state.user) {
    panel.actions.hidden = false;
    panel.delivery.hidden = false;
    panel.save.disabled = false;
    panel.refresh.disabled = false;
    updateAlertDeliveryFields(panel);
    showAlertMonitorStatus(panel, "Save daily alert rules or open recent alert runs.");
    return;
  }

  panel.actions.hidden = true;
  panel.delivery.hidden = true;
  panel.list.hidden = true;
  panel.save.disabled = true;
  panel.refresh.disabled = true;
  showAlertMonitorStatus(panel, "Sign in to save daily alert rules.");
}

function updateAlertDeliveryFields(panel) {
  const enabled = Boolean(panel.webhookEnabled?.checked);
  if (panel.webhookUrl) {
    panel.webhookUrl.disabled = !enabled;
  }
  if (panel.deliveryMinSeverity) {
    panel.deliveryMinSeverity.disabled = !enabled;
  }
}

function updateAccountControls(account, state) {
  account.signIn.disabled = !state.client || Boolean(state.user);
  account.signOut.disabled = !state.user;

  if (state.error) {
    account.root.dataset.accountState = "error";
    account.label.textContent = "Auth error";
    account.auth.hidden = true;
    account.session.hidden = true;
    showAccountStatus(account, state.error);
    return;
  }

  if (!state.ready) {
    account.root.dataset.accountState = "loading";
    account.label.textContent = "Checking";
    account.auth.hidden = true;
    account.session.hidden = true;
    showAccountStatus(account, "Checking account...");
    return;
  }

  if (state.user) {
    const email = state.user.email || "Signed in";
    account.root.dataset.accountState = "signed-in";
    account.label.textContent = "Signed in";
    account.auth.hidden = true;
    account.session.hidden = false;
    account.emailLabel.textContent = email;
    showAccountStatus(account, `Signed in as ${email}.`);
    return;
  }

  account.root.dataset.accountState = "signed-out";
  account.label.textContent = "Sign in";
  account.auth.hidden = false;
  account.session.hidden = true;
  showAccountStatus(account, "Sign in to save research across Terminal, Vision, Fax, and Moneyline.");
}

function showResearchStatus(panel, message) {
  panel.status.textContent = message;
}

function showSavedWatchlistStatus(panel, message) {
  panel.status.textContent = message;
}

function showAlertMonitorStatus(panel, message) {
  panel.status.textContent = message;
}

function showAccountStatus(account, message) {
  if (account.status) {
    account.status.textContent = message;
  }
}

function toggleAccountPanel(account) {
  const expanded = account.toggle.getAttribute("aria-expanded") === "true";
  if (expanded) {
    closeAccountPanel(account);
    return;
  }
  account.panel.hidden = false;
  account.toggle.setAttribute("aria-expanded", "true");
  if (authState.ready && !authState.user && !authState.error) {
    account.email.focus();
  }
}

function closeAccountPanel(account) {
  if (!account?.panel || !account.toggle) {
    return;
  }
  account.panel.hidden = true;
  account.toggle.setAttribute("aria-expanded", "false");
}

export function subscribeAuth(listener) {
  authListeners.add(listener);
  listener(authState);
  return () => authListeners.delete(listener);
}

function notifyAuth() {
  authListeners.forEach((listener) => listener(authState));
}

export async function initAuth() {
  if (!authPromise) {
    authPromise = setupAuth();
  }
  await authPromise;
}

async function setupAuth() {
  try {
    const config = await publicConfig();
    if (!config.supabase?.enabled) {
      authState.error = "Supabase is not configured.";
      authState.ready = true;
      notifyAuth();
      return;
    }
    if (!window.supabase?.createClient) {
      authState.error = "Supabase client did not load.";
      authState.ready = true;
      notifyAuth();
      return;
    }

    authState.client = window.supabase.createClient(
      config.supabase.url,
      config.supabase.anon_key,
      {
        auth: {
          autoRefreshToken: true,
          detectSessionInUrl: true,
          persistSession: true,
        },
      },
    );

    const { data } = await authState.client.auth.getSession();
    authState.user = data.session?.user || null;
    authState.ready = true;
    notifyAuth();

    authState.client.auth.onAuthStateChange((_event, session) => {
      authState.user = session?.user || null;
      authState.ready = true;
      notifyAuth();
    });
  } catch (error) {
    authState.error = error.message || "Config failed";
    authState.ready = true;
    notifyAuth();
  }
}

async function publicConfig() {
  if (!configPromise) {
    configPromise = fetch(CONFIG_ENDPOINT).then((response) => {
      if (!response.ok) {
        throw new Error("Could not load app config");
      }
      return response.json();
    });
  }
  return configPromise;
}

function relativeDate(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  return date.toLocaleDateString(undefined, { month: "short", day: "numeric" });
}

function normalizeTickerList(value) {
  if (Array.isArray(value)) {
    return value.map((ticker) => normalizeTicker(ticker)).filter(Boolean);
  }
  return String(value || "")
    .split(",")
    .map((ticker) => normalizeTicker(ticker))
    .filter(Boolean);
}

function normalizeTicker(value) {
  return String(value || "")
    .trim()
    .toUpperCase();
}

function clampNumber(value, minimum, maximum, fallback) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return fallback;
  }
  return Math.max(minimum, Math.min(number, maximum));
}

function firstValue(...values) {
  return values.find((value) => value !== undefined && value !== null && String(value).trim() !== "");
}

function firstNumber(...values) {
  for (const value of values) {
    const number = Number(value);
    if (Number.isFinite(number)) {
      return number;
    }
  }
  return null;
}

function formatTimelineCurrency(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "N/A";
  }
  return `$${number.toLocaleString(undefined, { maximumFractionDigits: 2 })}`;
}

function formatTimelinePercent(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) {
    return "N/A";
  }
  return `${formatCompactValue(number * 100)}%`;
}
