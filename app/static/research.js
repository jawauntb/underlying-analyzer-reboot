const CONFIG_ENDPOINT = "/api/config";
const RECENT_LIMIT = 5;

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

export function mountResearchLibrary({ insertAfter, getRecord, modeFilter, openRecord }) {
  if (!insertAfter) {
    return { setCanSave() {} };
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

  setupResearchLibrary(panel, state, { getRecord, modeFilter, openRecord });
  return controls;
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

function createResearchPanel() {
  const root = document.createElement("section");
  root.className = "research-panel";
  root.hidden = true;
  root.innerHTML = `
    <div class="research-head">
      <div>
        <div class="panel-label">Library</div>
        <p class="research-status" data-role="status">Sign in to save generated research.</p>
      </div>
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
    <div class="research-list" data-role="list" hidden></div>
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
  };
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
    panel.signOut.hidden = false;
    panel.save.disabled = !state.canSave;
    showResearchStatus(panel, `Signed in as ${state.user.email || "Supabase user"}.`);
    return;
  }

  panel.auth.hidden = false;
  panel.actions.hidden = true;
  panel.signOut.hidden = true;
  panel.save.disabled = true;
  showResearchStatus(panel, "Sign in to save generated research.");
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

function subscribeAuth(listener) {
  authListeners.add(listener);
  listener(authState);
  return () => authListeners.delete(listener);
}

function notifyAuth() {
  authListeners.forEach((listener) => listener(authState));
}

async function initAuth() {
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
