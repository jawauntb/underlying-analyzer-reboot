const CONFIG_ENDPOINT = "/api/config";
const RECENT_LIMIT = 5;

let configPromise = null;

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
  try {
    const config = await publicConfig();
    if (!config.supabase?.enabled) {
      return;
    }
    if (!window.supabase?.createClient) {
      showResearchStatus(panel, "Library unavailable: Supabase client did not load.");
      panel.root.hidden = false;
      return;
    }

    state.client = window.supabase.createClient(
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

    panel.root.hidden = false;
    bindResearchEvents(panel, state, callbacks);

    const { data } = await state.client.auth.getSession();
    state.user = data.session?.user || null;
    updateResearchControls(panel, state);
    if (state.user) {
      await loadRecentResearch(panel, state, callbacks, true);
    }

    state.client.auth.onAuthStateChange(async (_event, session) => {
      state.user = session?.user || null;
      updateResearchControls(panel, state);
      if (state.user) {
        await loadRecentResearch(panel, state, callbacks, true);
      }
    });
  } catch (error) {
    showResearchStatus(panel, `Library unavailable: ${error.message || "Config failed"}`);
    panel.root.hidden = false;
  }
}

function bindResearchEvents(panel, state, callbacks) {
  panel.email.addEventListener("keydown", (event) => {
    if (event.key === "Enter") {
      event.preventDefault();
      signIn(panel, state);
    }
  });
  panel.signIn.addEventListener("click", () => signIn(panel, state));
  panel.signOut.addEventListener("click", () => signOut(panel, state));
  panel.save.addEventListener("click", () => saveResearch(panel, state, callbacks));
  panel.recent.addEventListener("click", () => loadRecentResearch(panel, state, callbacks, false));
}

async function signIn(panel, state) {
  const email = panel.email.value.trim();
  if (!email) {
    showResearchStatus(panel, "Enter an email for a Supabase magic link.");
    panel.email.focus();
    return;
  }

  panel.signIn.disabled = true;
  showResearchStatus(panel, "Sending magic link...");
  const { error } = await state.client.auth.signInWithOtp({
    email,
    options: {
      emailRedirectTo: window.location.href.split("#")[0],
    },
  });
  panel.signIn.disabled = false;

  if (error) {
    showResearchStatus(panel, error.message);
    return;
  }
  showResearchStatus(panel, "Check your email, then return here signed in.");
}

async function signOut(panel, state) {
  await state.client.auth.signOut();
  state.user = null;
  panel.list.hidden = true;
  panel.list.innerHTML = "";
  updateResearchControls(panel, state);
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

function showResearchStatus(panel, message) {
  panel.status.textContent = message;
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
