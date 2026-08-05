/**
 * Research agent console.
 *
 * Streams NDJSON events from /api/agent/chat/stream and renders them as a
 * conversation: prose, tool cards with inline chart artifacts, and publishable
 * research articles. Conversations and saved briefs persist through
 * chat-store.js (Supabase when signed in, localStorage otherwise).
 */

import { mountAccountControls } from "./research.js";
import { renderMarkdown, escapeHtml } from "./markdown.js";
import {
  appendMessage,
  createChat,
  deleteArticle,
  deleteChat,
  initStore,
  listArticles,
  listChats,
  loadArticle,
  loadChat,
  renameChat,
  saveArticle,
  storeState,
  subscribeStore,
} from "./chat-store.js";

const STREAM_ENDPOINT = "/api/agent/chat/stream";
const TOOLS_ENDPOINT = "/api/agent/tools";

const SUGGESTIONS = [
  {
    title: "Read the tape on NVDA",
    detail: "Auction levels, trend health, and what changed this week",
    prompt:
      "Show me the auction and regression packs for NVDA on a 6 month window, then tell me where price is accepting or rejecting and what news moved it.",
  },
  {
    title: "Rank a watchlist",
    detail: "Cockpit ranking, then drill into the top names",
    prompt:
      "Rank AAPL, MSFT, NVDA, AMD, AVGO, and SMH by setup strength. Start with the cockpit, then look closer at the two strongest names.",
  },
  {
    title: "Find inflection names",
    detail: "Torque scan for coiled-spring setups",
    prompt:
      "Run a torque scan across TSLA, RIVN, F, GM, and LCID and explain which names are actually inflecting rather than just cheap.",
  },
  {
    title: "Write me a brief",
    detail: "A saveable research article with recommendations",
    prompt:
      "Research SMCI: pull the charts, check recent filings and news, then write a research brief with explicit recommendations and what would invalidate them.",
  },
];

const dom = {};
const state = {
  chatId: null,
  chatTitle: "New chat",
  messages: [],
  activeTab: "chats",
  chats: [],
  articles: [],
  streaming: false,
  controller: null,
  search: "",
  toolCatalog: null,
  savedArticleKeys: new Set(),
};

document.addEventListener("DOMContentLoaded", () => {
  cacheDom();
  bindEvents();
  renderSuggestions();
  mountAccountControls({ root: dom.accountControl });
  initStore().catch((error) => console.warn("Store init failed", error));
  subscribeStore(onStoreChange);
  loadToolCatalog();
});

function cacheDom() {
  dom.app = document.getElementById("chat-app");
  dom.sidebar = document.getElementById("chat-sidebar");
  dom.scrim = document.getElementById("chat-scrim");
  dom.openSidebar = document.getElementById("sidebar-open");
  dom.closeSidebar = document.getElementById("sidebar-close");
  dom.newChat = document.getElementById("new-chat");
  dom.tabChats = document.getElementById("tab-chats");
  dom.tabBriefs = document.getElementById("tab-briefs");
  dom.search = document.getElementById("sidebar-search");
  dom.chatList = document.getElementById("chat-list");
  dom.briefList = document.getElementById("brief-list");
  dom.accountControl = document.getElementById("account-control");
  dom.storageNote = document.getElementById("storage-note");
  dom.title = document.getElementById("chat-title");
  dom.subtitle = document.getElementById("chat-subtitle");
  dom.toolsButton = document.getElementById("tools-button");
  dom.toolsCount = document.getElementById("tools-count");
  dom.agentDot = document.getElementById("agent-dot");
  dom.toolsPanel = document.getElementById("tools-panel");
  dom.toolsGrid = document.getElementById("tools-grid");
  dom.thread = document.getElementById("chat-thread");
  dom.welcome = document.getElementById("chat-welcome");
  dom.suggestions = document.getElementById("chat-suggestions");
  dom.composer = document.getElementById("chat-composer");
  dom.input = document.getElementById("chat-input");
  dom.send = document.getElementById("chat-send");
  dom.stop = document.getElementById("chat-stop");
  dom.viewer = document.getElementById("chat-viewer");
  dom.viewerBody = document.getElementById("viewer-body");
  dom.viewerClose = document.getElementById("viewer-close");
}

function bindEvents() {
  dom.openSidebar.addEventListener("click", () => toggleDrawer(true));
  dom.closeSidebar.addEventListener("click", () => toggleDrawer(false));
  dom.scrim.addEventListener("click", () => toggleDrawer(false));
  dom.newChat.addEventListener("click", startNewChat);
  dom.tabChats.addEventListener("click", () => setTab("chats"));
  dom.tabBriefs.addEventListener("click", () => setTab("briefs"));

  dom.search.addEventListener("input", () => {
    state.search = dom.search.value.trim().toLowerCase();
    renderSidebarList();
  });

  dom.toolsButton.addEventListener("click", () => {
    const open = dom.toolsPanel.hidden;
    dom.toolsPanel.hidden = !open;
    dom.toolsButton.setAttribute("aria-expanded", String(open));
  });

  dom.composer.addEventListener("submit", (event) => {
    event.preventDefault();
    sendMessage();
  });

  dom.input.addEventListener("input", autosize);
  dom.input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey && !isCoarsePointer()) {
      event.preventDefault();
      sendMessage();
    }
  });

  dom.stop.addEventListener("click", () => {
    if (state.controller) {
      state.controller.abort();
    }
  });

  dom.viewerClose.addEventListener("click", closeViewer);
  dom.viewer.addEventListener("click", (event) => {
    if (event.target === dom.viewer) {
      closeViewer();
    }
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") {
      closeViewer();
      toggleDrawer(false);
    }
  });
}

function isCoarsePointer() {
  return window.matchMedia("(pointer: coarse)").matches;
}

/* ------------------------------------------------------------- catalog */

async function loadToolCatalog() {
  try {
    const response = await fetch(TOOLS_ENDPOINT);
    if (!response.ok) {
      throw new Error(`Catalog unavailable (${response.status})`);
    }
    const catalog = await response.json();
    state.toolCatalog = catalog;
    dom.toolsCount.textContent = `${catalog.tool_count} tools`;
    dom.agentDot.classList.toggle("ok", Boolean(catalog.agent_ready));
    dom.agentDot.classList.toggle("warn", !catalog.agent_ready);
    if (!catalog.agent_ready) {
      dom.subtitle.textContent =
        "Agent offline: ANTHROPIC_API_KEY is not configured on this deployment.";
    }
    renderToolCatalog(catalog);
  } catch (error) {
    dom.toolsCount.textContent = "tools";
    dom.agentDot.classList.add("warn");
    console.warn(error);
  }
}

function renderToolCatalog(catalog) {
  dom.toolsGrid.innerHTML = catalog.tools
    .map(
      (tool) => `
        <article class="chat-tool-card">
          <h4>${escapeHtml(tool.title)}</h4>
          <p>${escapeHtml(tool.summary)}</p>
          <span class="chat-tool-cost">${escapeHtml(tool.group)} &middot; ${escapeHtml(tool.cost)}</span>
        </article>`,
    )
    .join("");
}

/* --------------------------------------------------------------- store */

function onStoreChange(snapshot) {
  dom.storageNote.textContent = snapshot.user
    ? "Synced to your account"
    : "Saving locally on this device";
  refreshSidebar();
}

async function refreshSidebar() {
  try {
    const [chats, articles] = await Promise.all([listChats(), listArticles()]);
    state.chats = chats;
    state.articles = articles;
    renderSidebarList();
  } catch (error) {
    console.warn("Could not load history", error);
  }
}

function setTab(tab) {
  state.activeTab = tab;
  dom.tabChats.classList.toggle("is-active", tab === "chats");
  dom.tabBriefs.classList.toggle("is-active", tab === "briefs");
  dom.tabChats.setAttribute("aria-selected", String(tab === "chats"));
  dom.tabBriefs.setAttribute("aria-selected", String(tab === "briefs"));
  dom.chatList.hidden = tab !== "chats";
  dom.briefList.hidden = tab !== "briefs";
  dom.search.placeholder = tab === "chats" ? "Search chats" : "Search briefs";
  renderSidebarList();
}

function renderSidebarList() {
  const term = state.search;
  if (state.activeTab === "chats") {
    const items = state.chats.filter(
      (chat) => !term || String(chat.title || "").toLowerCase().includes(term),
    );
    dom.chatList.innerHTML = items.length
      ? ""
      : '<p class="chat-list-empty">No conversations yet.</p>';
    items.forEach((chat) => dom.chatList.append(chatListItem(chat)));
    return;
  }

  const items = state.articles.filter((article) => {
    if (!term) {
      return true;
    }
    const haystack = `${article.title} ${(article.tickers || []).join(" ")}`.toLowerCase();
    return haystack.includes(term);
  });
  dom.briefList.innerHTML = items.length
    ? ""
    : '<p class="chat-list-empty">Saved briefs will appear here.</p>';
  items.forEach((article) => dom.briefList.append(articleListItem(article)));
}

function chatListItem(chat) {
  const row = document.createElement("div");
  row.className = `chat-list-item${chat.id === state.chatId ? " is-active" : ""}`;

  const open = document.createElement("button");
  open.type = "button";
  open.className = "chat-list-text";
  open.innerHTML = `
    <span class="chat-list-title">${escapeHtml(chat.title || "Untitled")}</span>
    <span class="chat-list-meta">${escapeHtml(relativeTime(chat.updated_at || chat.created_at))}</span>`;
  open.addEventListener("click", () => openChat(chat.id));

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "chat-list-delete";
  remove.innerHTML = "&#215;";
  remove.setAttribute("aria-label", `Delete ${chat.title || "chat"}`);
  remove.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (!window.confirm("Delete this conversation?")) {
      return;
    }
    await deleteChat(chat.id);
    if (state.chatId === chat.id) {
      startNewChat();
    }
    refreshSidebar();
  });

  row.append(open, remove);
  return row;
}

function articleListItem(article) {
  const row = document.createElement("div");
  row.className = "chat-list-item";

  const open = document.createElement("button");
  open.type = "button";
  open.className = "chat-list-text";
  const tickers = (article.tickers || []).join(" ");
  open.innerHTML = `
    <span class="chat-list-title">${escapeHtml(article.title)}</span>
    <span class="chat-list-meta">${escapeHtml(
      [tickers, relativeTime(article.created_at)].filter(Boolean).join(" · "),
    )}</span>`;
  open.addEventListener("click", () => openSavedArticle(article.id));

  const remove = document.createElement("button");
  remove.type = "button";
  remove.className = "chat-list-delete";
  remove.innerHTML = "&#215;";
  remove.setAttribute("aria-label", `Delete ${article.title}`);
  remove.addEventListener("click", async (event) => {
    event.stopPropagation();
    if (!window.confirm("Delete this brief?")) {
      return;
    }
    await deleteArticle(article.id);
    refreshSidebar();
  });

  row.append(open, remove);
  return row;
}

async function openSavedArticle(id) {
  try {
    const record = await loadArticle(id);
    if (!record) {
      return;
    }
    const article = record.article || record;
    dom.viewerBody.innerHTML = "";
    dom.viewerBody.append(articleCard(article, { saved: true }));
    dom.viewer.hidden = false;
    toggleDrawer(false);
  } catch (error) {
    console.warn("Could not open brief", error);
  }
}

/* ----------------------------------------------------------- chat flow */

function startNewChat() {
  if (state.streaming && state.controller) {
    state.controller.abort();
  }
  state.chatId = null;
  state.chatTitle = "New chat";
  state.messages = [];
  state.savedArticleKeys = new Set();
  dom.title.textContent = "New chat";
  dom.subtitle.textContent = "Ask about a ticker, a watchlist, or a thesis";
  dom.thread.innerHTML = "";
  dom.thread.append(dom.welcome);
  dom.welcome.hidden = false;
  toggleDrawer(false);
  renderSidebarList();
  dom.input.focus();
}

async function openChat(id) {
  try {
    const chat = await loadChat(id);
    if (!chat) {
      return;
    }
    state.chatId = chat.id;
    state.chatTitle = chat.title;
    state.messages = chat.messages || [];
    state.savedArticleKeys = new Set();
    dom.title.textContent = chat.title;
    dom.subtitle.textContent = `${state.messages.length} messages`;
    dom.thread.innerHTML = "";
    dom.welcome.hidden = true;

    state.messages.forEach((message) => {
      if (message.role === "user") {
        dom.thread.append(userTurn(message.content));
        return;
      }
      const turn = assistantTurn();
      if (message.artifacts?.length) {
        turn.answer.append(figureGroup(message.artifacts));
      }
      if (message.content) {
        turn.prose.innerHTML = renderMarkdown(message.content);
      }
      if (message.article) {
        state.savedArticleKeys.add(articleKey(message.article));
        turn.answer.append(articleCard(message.article, { saved: true }));
      }
      dom.thread.append(turn.root);
    });

    toggleDrawer(false);
    renderSidebarList();
    scrollToEnd(true);
  } catch (error) {
    console.warn("Could not open conversation", error);
  }
}

function renderSuggestions() {
  dom.suggestions.innerHTML = "";
  SUGGESTIONS.forEach((suggestion) => {
    const button = document.createElement("button");
    button.type = "button";
    button.className = "chat-suggestion";
    button.innerHTML = `<strong>${escapeHtml(suggestion.title)}</strong><span>${escapeHtml(
      suggestion.detail,
    )}</span>`;
    button.addEventListener("click", () => {
      dom.input.value = suggestion.prompt;
      autosize();
      sendMessage();
    });
    dom.suggestions.append(button);
  });
}

function autosize() {
  dom.input.style.height = "auto";
  dom.input.style.height = `${Math.min(dom.input.scrollHeight, 192)}px`;
  dom.send.disabled = !dom.input.value.trim() || state.streaming;
}

async function sendMessage() {
  const text = dom.input.value.trim();
  if (!text || state.streaming) {
    return;
  }

  dom.input.value = "";
  autosize();
  dom.welcome.hidden = true;

  dom.thread.append(userTurn(text));
  scrollToEnd();

  const userMessage = { role: "user", content: text };
  state.messages.push(userMessage);

  if (!state.chatId) {
    try {
      const chat = await createChat(deriveTitle(text));
      state.chatId = chat.id;
      state.chatTitle = chat.title;
      dom.title.textContent = chat.title;
      refreshSidebar();
    } catch (error) {
      console.warn("Could not create conversation", error);
    }
  }
  persist(userMessage);

  await streamTurn();
}

async function streamTurn() {
  const turn = assistantTurn();
  dom.thread.append(turn.root);
  const thinking = thinkingIndicator();
  turn.answer.append(thinking);
  scrollToEnd();

  setStreaming(true);
  const controller = new AbortController();
  state.controller = controller;

  const collected = {
    text: "",
    toolTrace: [],
    artifacts: [],
    article: null,
  };
  const toolCards = new Map();

  try {
    const response = await fetch(STREAM_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ messages: historyPayload() }),
      signal: controller.signal,
    });

    if (!response.ok) {
      const payload = await response.json().catch(() => ({}));
      throw new Error(payload.error || `Request failed (${response.status})`);
    }

    for await (const event of readNdjson(response, controller.signal)) {
      thinking.remove();
      handleEvent(event, turn, collected, toolCards);
    }
  } catch (error) {
    thinking.remove();
    if (error.name !== "AbortError") {
      turn.answer.append(errorBanner(error.message || "The agent run failed."));
    } else if (collected.text) {
      turn.answer.append(errorBanner("Stopped."));
    }
  } finally {
    thinking.remove();
    setStreaming(false);
    state.controller = null;
  }

  if (collected.text || collected.article || collected.artifacts.length) {
    const message = {
      role: "assistant",
      content: collected.text,
      tool_trace: collected.toolTrace,
      artifacts: collected.artifacts,
      article: collected.article,
    };
    state.messages.push(message);
    persist(message);
  }
  scrollToEnd();
}

function handleEvent(event, turn, collected, toolCards) {
  switch (event.type) {
    case "text": {
      collected.text += event.text || "";
      turn.prose.innerHTML = renderMarkdown(collected.text);
      scrollToEnd();
      break;
    }
    case "tool_call": {
      const card = toolCard(event);
      toolCards.set(event.id, card);
      turn.answer.insertBefore(card.root, turn.prose);
      scrollToEnd();
      break;
    }
    case "tool_result": {
      const card = toolCards.get(event.id);
      if (card) {
        card.complete(event);
      }
      if (event.artifacts?.length) {
        collected.artifacts.push(...event.artifacts);
        turn.answer.insertBefore(figureGroup(event.artifacts), turn.prose);
      }
      collected.toolTrace.push(
        `${event.name} -> ${event.ok ? "ok" : `error: ${event.error || "failed"}`}`,
      );
      scrollToEnd();
      break;
    }
    case "article": {
      collected.article = event.article;
      turn.answer.append(articleCard(event.article, { markdown: event.markdown }));
      scrollToEnd();
      break;
    }
    case "error": {
      turn.answer.append(errorBanner(event.message || "The agent run failed."));
      break;
    }
    default:
      break;
  }
}

function historyPayload() {
  return state.messages.map((message) => ({
    role: message.role,
    content: message.content,
    tool_trace: message.tool_trace || [],
  }));
}

function persist(message) {
  if (!state.chatId) {
    return;
  }
  appendMessage(state.chatId, message)
    .then(() => refreshSidebar())
    .catch((error) => console.warn("Could not save message", error));
}

function setStreaming(streaming) {
  state.streaming = streaming;
  dom.stop.hidden = !streaming;
  dom.send.hidden = streaming;
  dom.send.disabled = streaming || !dom.input.value.trim();
}

/**
 * Reads an NDJSON body incrementally. The server pads the stream with newlines
 * to defeat proxy buffering, so blank lines are expected and skipped.
 */
async function* readNdjson(response, signal) {
  const reader = response.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";

  try {
    while (true) {
      if (signal?.aborted) {
        return;
      }
      const { done, value } = await reader.read();
      if (done) {
        break;
      }
      buffer += decoder.decode(value, { stream: true });
      const lines = buffer.split("\n");
      buffer = lines.pop() ?? "";
      for (const line of lines) {
        const trimmed = line.trim();
        if (!trimmed) {
          continue;
        }
        try {
          yield JSON.parse(trimmed);
        } catch (error) {
          console.warn("Skipped malformed stream line", error);
        }
      }
    }
    const tail = buffer.trim();
    if (tail) {
      try {
        yield JSON.parse(tail);
      } catch (error) {
        console.warn("Skipped malformed stream tail", error);
      }
    }
  } finally {
    reader.cancel().catch(() => {});
  }
}

/* --------------------------------------------------------------- views */

function userTurn(text) {
  const root = document.createElement("div");
  root.className = "chat-turn is-user";
  const bubble = document.createElement("div");
  bubble.className = "chat-bubble";
  bubble.textContent = text;
  root.append(bubble);
  return root;
}

function assistantTurn() {
  const root = document.createElement("div");
  root.className = "chat-turn is-assistant";

  const avatar = document.createElement("div");
  avatar.className = "chat-avatar";
  avatar.setAttribute("aria-hidden", "true");

  const answer = document.createElement("div");
  answer.className = "chat-answer";

  const prose = document.createElement("div");
  prose.className = "chat-prose";
  answer.append(prose);

  root.append(avatar, answer);
  return { root, answer, prose };
}

function thinkingIndicator() {
  const node = document.createElement("div");
  node.className = "chat-thinking";
  node.innerHTML = "<span></span><span></span><span></span>";
  return node;
}

function errorBanner(message) {
  const node = document.createElement("p");
  node.className = "chat-error";
  node.textContent = message;
  return node;
}

function toolCard(event) {
  const root = document.createElement("div");
  root.className = "tool-card is-running";

  const head = document.createElement("button");
  head.type = "button";
  head.className = "tool-card-head";
  head.innerHTML = `
    <span class="tool-status" aria-hidden="true"></span>
    <span class="tool-card-label">
      <strong>${escapeHtml(event.title || event.name)}</strong>
      <span>${escapeHtml(summarizeArgs(event.input))}</span>
    </span>
    <span class="tool-card-timing"></span>
    <span class="tool-card-caret" aria-hidden="true">&#9656;</span>`;

  const body = document.createElement("div");
  body.className = "tool-card-body";
  body.hidden = true;

  head.addEventListener("click", () => {
    body.hidden = !body.hidden;
    root.classList.toggle("is-open", !body.hidden);
  });

  root.append(head, body);

  return {
    root,
    complete(result) {
      root.classList.remove("is-running");
      root.classList.add(result.ok ? "is-done" : "is-error");
      const timing = head.querySelector(".tool-card-timing");
      timing.textContent = result.duration_ms ? `${formatDuration(result.duration_ms)}` : "";
      if (result.ok) {
        const pre = document.createElement("pre");
        pre.textContent = JSON.stringify(result.result, null, 2);
        body.append(pre);
      } else {
        const error = document.createElement("p");
        error.className = "tool-error";
        error.textContent = result.error || "Tool call failed";
        root.append(error);
      }
    },
  };
}

function figureGroup(artifacts) {
  const wrap = document.createElement("div");
  wrap.className = "tool-figures";
  artifacts.forEach((artifact) => {
    if (!artifact.data) {
      return;
    }
    const figure = document.createElement("figure");
    figure.className = "tool-figure";

    const img = document.createElement("img");
    img.src = `data:${artifact.mime || "image/png"};base64,${artifact.data}`;
    img.alt = artifact.title || "Rendered chart";
    img.loading = "lazy";
    img.addEventListener("click", () => openViewer(img.src, img.alt));
    figure.append(img);

    const caption = artifact.caption || artifact.title;
    if (caption) {
      const figcaption = document.createElement("figcaption");
      figcaption.textContent = caption;
      figure.append(figcaption);
    }
    wrap.append(figure);
  });
  return wrap;
}

function articleCard(article, { markdown, saved } = {}) {
  const root = document.createElement("article");
  root.className = "article-card";

  const head = document.createElement("header");
  head.className = "article-head";
  head.innerHTML = `
    <p class="article-kicker">Research brief</p>
    <h3>${escapeHtml(article.title)}</h3>
    ${article.subtitle ? `<p>${escapeHtml(article.subtitle)}</p>` : ""}`;
  if (article.tickers?.length) {
    const tickers = document.createElement("div");
    tickers.className = "article-tickers";
    tickers.innerHTML = article.tickers
      .map((ticker) => `<span class="article-ticker">${escapeHtml(ticker)}</span>`)
      .join("");
    head.append(tickers);
  }

  const body = document.createElement("div");
  body.className = "article-body";

  const thesis = document.createElement("p");
  thesis.className = "article-thesis";
  thesis.textContent = article.thesis;
  body.append(thesis);

  (article.sections || []).forEach((section) => {
    const wrap = document.createElement("section");
    wrap.className = "article-section";
    wrap.innerHTML = `<h4>${escapeHtml(section.heading)}</h4><div class="chat-prose">${renderMarkdown(
      section.body,
    )}</div>`;
    body.append(wrap);
  });

  if (article.recommendations?.length) {
    const wrap = document.createElement("section");
    wrap.className = "article-section";
    wrap.innerHTML = "<h4>Recommendations</h4>";
    const list = document.createElement("ul");
    list.className = "rec-list";
    article.recommendations.forEach((rec) => {
      const item = document.createElement("li");
      item.className = "rec-item";
      item.innerHTML = `
        <div class="rec-top">
          ${rec.ticker ? `<span class="rec-ticker">${escapeHtml(rec.ticker)}</span>` : ""}
          <span class="rec-stance is-${escapeHtml(rec.stance)}">${escapeHtml(rec.stance)}</span>
          ${rec.confidence ? `<span class="rec-confidence">${escapeHtml(rec.confidence)} confidence</span>` : ""}
        </div>
        <p class="rec-action">${escapeHtml(rec.action)}</p>
        ${rec.rationale ? `<p class="rec-detail">${escapeHtml(rec.rationale)}</p>` : ""}
        ${
          rec.invalidation
            ? `<p class="rec-detail"><strong>Invalidated if:</strong> ${escapeHtml(rec.invalidation)}</p>`
            : ""
        }`;
      list.append(item);
    });
    wrap.append(list);
    body.append(wrap);
  }

  if (article.risks?.length) {
    const wrap = document.createElement("section");
    wrap.className = "article-section";
    wrap.innerHTML = `<h4>Risks</h4><div class="chat-prose">${renderMarkdown(
      article.risks.map((risk) => `- ${risk}`).join("\n"),
    )}</div>`;
    body.append(wrap);
  }

  if (article.sources?.length) {
    const wrap = document.createElement("section");
    wrap.className = "article-section";
    const lines = article.sources
      .map((source) =>
        source.url ? `- [${source.label}](${source.url})` : `- ${source.label}`,
      )
      .join("\n");
    wrap.innerHTML = `<h4>Sources</h4><div class="chat-prose">${renderMarkdown(lines)}</div>`;
    body.append(wrap);
  }

  root.append(head, body, articleActions(article, { markdown, saved }));
  return root;
}

function articleActions(article, { markdown, saved }) {
  const foot = document.createElement("footer");
  foot.className = "article-foot";
  const text = markdown || fallbackMarkdown(article);
  const key = articleKey(article);
  const alreadySaved = saved || state.savedArticleKeys.has(key);

  const save = document.createElement("button");
  save.type = "button";
  save.className = "article-action is-primary";
  save.textContent = alreadySaved ? "Saved" : "Save brief";
  save.disabled = alreadySaved;
  save.addEventListener("click", async () => {
    save.disabled = true;
    save.textContent = "Saving...";
    try {
      await saveArticle({
        article,
        markdown: text,
        summary: article.thesis,
        chatId: state.chatId,
      });
      state.savedArticleKeys.add(key);
      save.textContent = "Saved";
      refreshSidebar();
    } catch (error) {
      save.disabled = false;
      save.textContent = "Save failed";
      console.warn("Could not save brief", error);
    }
  });

  const copy = document.createElement("button");
  copy.type = "button";
  copy.className = "article-action";
  copy.textContent = "Copy markdown";
  copy.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(text);
      copy.textContent = "Copied";
      window.setTimeout(() => {
        copy.textContent = "Copy markdown";
      }, 1600);
    } catch (error) {
      copy.textContent = "Copy failed";
    }
  });

  const download = document.createElement("button");
  download.type = "button";
  download.className = "article-action";
  download.textContent = "Download";
  download.addEventListener("click", () => {
    const blob = new Blob([text], { type: "text/markdown" });
    const url = URL.createObjectURL(blob);
    const link = document.createElement("a");
    link.href = url;
    link.download = `${slug(article.title)}.md`;
    link.click();
    URL.revokeObjectURL(url);
  });

  foot.append(save, copy, download);
  return foot;
}

function openViewer(src, alt) {
  dom.viewerBody.innerHTML = "";
  const img = document.createElement("img");
  img.src = src;
  img.alt = alt || "";
  dom.viewerBody.append(img);
  dom.viewer.hidden = false;
}

function closeViewer() {
  dom.viewer.hidden = true;
  dom.viewerBody.innerHTML = "";
}

function toggleDrawer(open) {
  dom.app.classList.toggle("is-drawer-open", open);
  dom.scrim.hidden = !open;
}

/* ------------------------------------------------------------- helpers */

function scrollToEnd(instant = false) {
  window.requestAnimationFrame(() => {
    dom.thread.scrollTo({
      top: dom.thread.scrollHeight,
      behavior: instant ? "auto" : "smooth",
    });
  });
}

function summarizeArgs(input) {
  if (!input || typeof input !== "object") {
    return "";
  }
  return Object.entries(input)
    .slice(0, 3)
    .map(([key, value]) => {
      const text = Array.isArray(value) ? value.join(", ") : String(value);
      return `${key}: ${text.length > 32 ? `${text.slice(0, 29)}...` : text}`;
    })
    .join(" · ");
}

function formatDuration(ms) {
  return ms < 1000 ? `${ms}ms` : `${(ms / 1000).toFixed(1)}s`;
}

function deriveTitle(text) {
  const clean = text.replace(/\s+/g, " ").trim();
  return clean.length > 60 ? `${clean.slice(0, 57)}...` : clean;
}

function articleKey(article) {
  return `${article.title}::${article.generated_at || ""}`;
}

function slug(value) {
  return (
    String(value || "brief")
      .toLowerCase()
      .replace(/[^a-z0-9]+/g, "-")
      .replace(/^-|-$/g, "")
      .slice(0, 60) || "brief"
  );
}

function fallbackMarkdown(article) {
  const lines = [`# ${article.title}`, "", article.thesis, ""];
  (article.sections || []).forEach((section) => {
    lines.push(`## ${section.heading}`, "", section.body, "");
  });
  return lines.join("\n");
}

function relativeTime(value) {
  if (!value) {
    return "";
  }
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) {
    return "";
  }
  const seconds = Math.round((Date.now() - date.getTime()) / 1000);
  if (seconds < 60) {
    return "just now";
  }
  const minutes = Math.round(seconds / 60);
  if (minutes < 60) {
    return `${minutes}m ago`;
  }
  const hours = Math.round(minutes / 60);
  if (hours < 24) {
    return `${hours}h ago`;
  }
  const days = Math.round(hours / 24);
  if (days < 30) {
    return `${days}d ago`;
  }
  return date.toLocaleDateString();
}

export { renderMarkdown, storeState };
