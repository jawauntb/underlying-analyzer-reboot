/**
 * Conversation and article persistence.
 *
 * Two interchangeable backends behind one interface:
 *
 * - Supabase (`agent_chats`, `agent_messages`, `research_articles`) once the
 *   visitor is signed in. Row level security scopes everything to their user id.
 * - localStorage otherwise, so the console is fully usable signed out and a
 *   conversation is never lost just because someone has not clicked sign in.
 *
 * When a visitor signs in, whatever is sitting in local storage is migrated up
 * once, then local storage is cleared.
 */

import { initAuth, subscribeAuth } from "./research.js";

const LOCAL_CHATS = "underlying.chat.conversations.v1";
const LOCAL_ARTICLES = "underlying.chat.articles.v1";
const MIGRATED_FLAG = "underlying.chat.migrated.v1";
const CHAT_LIMIT = 100;
const ARTICLE_LIMIT = 100;

const state = {
  client: null,
  user: null,
  ready: false,
  error: null,
};

const listeners = new Set();

export function subscribeStore(listener) {
  listeners.add(listener);
  listener(storeState());
  return () => listeners.delete(listener);
}

export function storeState() {
  return {
    ready: state.ready,
    error: state.error,
    user: state.user,
    backend: state.user ? "supabase" : "local",
  };
}

function notify() {
  const snapshot = storeState();
  listeners.forEach((listener) => {
    try {
      listener(snapshot);
    } catch (error) {
      console.error("chat store listener failed", error);
    }
  });
}

export async function initStore() {
  subscribeAuth(async (auth) => {
    const signedIn = Boolean(auth.user) && !state.user;
    state.client = auth.client;
    state.user = auth.user;
    state.ready = auth.ready;
    state.error = auth.error;
    if (signedIn && auth.client) {
      await migrateLocal();
    }
    notify();
  });
  await initAuth();
}

/* ------------------------------------------------------------------ chats */

export async function listChats() {
  if (!remote()) {
    return readLocal(LOCAL_CHATS)
      .map((chat) => ({ ...chat, messages: undefined }))
      .sort(byUpdated);
  }
  const { data, error } = await state.client
    .from("agent_chats")
    .select("id, title, updated_at, created_at")
    .order("updated_at", { ascending: false })
    .limit(CHAT_LIMIT);
  if (error) {
    throw new Error(error.message);
  }
  return data || [];
}

export async function loadChat(id) {
  if (!id) {
    return null;
  }
  if (!remote()) {
    return readLocal(LOCAL_CHATS).find((chat) => chat.id === id) || null;
  }
  const { data: chat, error } = await state.client
    .from("agent_chats")
    .select("id, title, created_at, updated_at")
    .eq("id", id)
    .maybeSingle();
  if (error) {
    throw new Error(error.message);
  }
  if (!chat) {
    return null;
  }
  const { data: messages, error: messageError } = await state.client
    .from("agent_messages")
    .select("id, role, content, tool_trace, artifacts, article, created_at")
    .eq("chat_id", id)
    .order("created_at", { ascending: true });
  if (messageError) {
    throw new Error(messageError.message);
  }
  return { ...chat, messages: (messages || []).map(fromRow) };
}

export async function createChat(title) {
  const name = cleanTitle(title);
  if (!remote()) {
    const chat = {
      id: localId(),
      title: name,
      messages: [],
      created_at: new Date().toISOString(),
      updated_at: new Date().toISOString(),
      message_count: 0,
    };
    const chats = readLocal(LOCAL_CHATS);
    chats.unshift(chat);
    writeLocal(LOCAL_CHATS, chats.slice(0, CHAT_LIMIT));
    return chat;
  }
  const { data, error } = await state.client
    .from("agent_chats")
    .insert({ title: name, user_id: state.user.id })
    .select("id, title, created_at, updated_at")
    .single();
  if (error) {
    throw new Error(error.message);
  }
  return { ...data, messages: [] };
}

export async function appendMessage(chatId, message) {
  const record = {
    role: message.role,
    content: message.content || "",
    tool_trace: message.tool_trace || [],
    artifacts: (message.artifacts || []).map(stripArtifactData),
    article: message.article || null,
  };

  if (!remote()) {
    const chats = readLocal(LOCAL_CHATS);
    const chat = chats.find((entry) => entry.id === chatId);
    if (!chat) {
      return null;
    }
    const stored = { ...record, id: localId(), created_at: new Date().toISOString() };
    chat.messages = [...(chat.messages || []), stored];
    chat.message_count = chat.messages.length;
    chat.updated_at = stored.created_at;
    writeLocal(LOCAL_CHATS, chats);
    return stored;
  }

  const { data, error } = await state.client
    .from("agent_messages")
    .insert({ ...record, chat_id: chatId, user_id: state.user.id })
    .select("id, role, content, tool_trace, artifacts, article, created_at")
    .single();
  if (error) {
    throw new Error(error.message);
  }
  await state.client
    .from("agent_chats")
    .update({ updated_at: new Date().toISOString() })
    .eq("id", chatId);
  return fromRow(data);
}

export async function renameChat(chatId, title) {
  const name = cleanTitle(title);
  if (!remote()) {
    const chats = readLocal(LOCAL_CHATS);
    const chat = chats.find((entry) => entry.id === chatId);
    if (chat) {
      chat.title = name;
      writeLocal(LOCAL_CHATS, chats);
    }
    return name;
  }
  const { error } = await state.client
    .from("agent_chats")
    .update({ title: name })
    .eq("id", chatId);
  if (error) {
    throw new Error(error.message);
  }
  return name;
}

export async function deleteChat(chatId) {
  if (!remote()) {
    writeLocal(
      LOCAL_CHATS,
      readLocal(LOCAL_CHATS).filter((chat) => chat.id !== chatId),
    );
    return;
  }
  const { error } = await state.client.from("agent_chats").delete().eq("id", chatId);
  if (error) {
    throw new Error(error.message);
  }
}

/* --------------------------------------------------------------- articles */

export async function listArticles() {
  if (!remote()) {
    return readLocal(LOCAL_ARTICLES).sort(byCreated);
  }
  const { data, error } = await state.client
    .from("research_articles")
    .select("id, title, subtitle, summary, tickers, created_at, chat_id")
    .order("created_at", { ascending: false })
    .limit(ARTICLE_LIMIT);
  if (error) {
    throw new Error(error.message);
  }
  return data || [];
}

export async function loadArticle(id) {
  if (!remote()) {
    return readLocal(LOCAL_ARTICLES).find((article) => article.id === id) || null;
  }
  const { data, error } = await state.client
    .from("research_articles")
    .select("id, title, subtitle, summary, tickers, article, markdown, created_at, chat_id")
    .eq("id", id)
    .maybeSingle();
  if (error) {
    throw new Error(error.message);
  }
  return data;
}

export async function saveArticle({ article, markdown, summary, chatId }) {
  const record = {
    title: article.title,
    subtitle: article.subtitle || null,
    summary: summary || article.thesis || "",
    tickers: article.tickers || [],
    article,
    markdown: markdown || "",
    chat_id: isUuid(chatId) ? chatId : null,
  };

  if (!remote()) {
    const stored = { ...record, id: localId(), created_at: new Date().toISOString() };
    const articles = readLocal(LOCAL_ARTICLES);
    articles.unshift(stored);
    writeLocal(LOCAL_ARTICLES, articles.slice(0, ARTICLE_LIMIT));
    return stored;
  }

  const { data, error } = await state.client
    .from("research_articles")
    .insert({ ...record, user_id: state.user.id })
    .select("id, title, subtitle, summary, tickers, created_at, chat_id")
    .single();
  if (error) {
    throw new Error(error.message);
  }
  return data;
}

export async function deleteArticle(id) {
  if (!remote()) {
    writeLocal(
      LOCAL_ARTICLES,
      readLocal(LOCAL_ARTICLES).filter((article) => article.id !== id),
    );
    return;
  }
  const { error } = await state.client.from("research_articles").delete().eq("id", id);
  if (error) {
    throw new Error(error.message);
  }
}

/* ---------------------------------------------------------------- helpers */

function remote() {
  return Boolean(state.client && state.user);
}

async function migrateLocal() {
  if (window.localStorage.getItem(MIGRATED_FLAG) === "1") {
    return;
  }
  const chats = readLocal(LOCAL_CHATS);
  const articles = readLocal(LOCAL_ARTICLES);
  if (!chats.length && !articles.length) {
    window.localStorage.setItem(MIGRATED_FLAG, "1");
    return;
  }

  try {
    for (const chat of chats.slice(0, 25)) {
      const created = await createChat(chat.title);
      for (const message of chat.messages || []) {
        await appendMessage(created.id, message);
      }
    }
    for (const article of articles.slice(0, 25)) {
      await saveArticle({
        article: article.article || article,
        markdown: article.markdown,
        summary: article.summary,
        chatId: null,
      });
    }
    window.localStorage.removeItem(LOCAL_CHATS);
    window.localStorage.removeItem(LOCAL_ARTICLES);
    window.localStorage.setItem(MIGRATED_FLAG, "1");
  } catch (error) {
    console.warn("Could not migrate local conversations", error);
  }
}

function fromRow(row) {
  return {
    id: row.id,
    role: row.role,
    content: row.content || "",
    tool_trace: row.tool_trace || [],
    artifacts: row.artifacts || [],
    article: row.article || null,
    created_at: row.created_at,
  };
}

/**
 * Saved messages keep artifact metadata but drop the base64 payload. A single
 * chart is ~200KB; keeping them would blow past localStorage quota within a few
 * conversations and bloat every history read.
 */
function stripArtifactData(artifact) {
  return {
    id: artifact.id,
    mime: artifact.mime,
    title: artifact.title,
    caption: artifact.caption,
    filename: artifact.filename,
  };
}

function cleanTitle(title) {
  const text = String(title || "").trim().replace(/\s+/g, " ");
  if (!text) {
    return "New conversation";
  }
  return text.length > 80 ? `${text.slice(0, 77)}...` : text;
}

function readLocal(key) {
  try {
    const raw = window.localStorage.getItem(key);
    const parsed = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? parsed : [];
  } catch (error) {
    return [];
  }
}

function writeLocal(key, value) {
  try {
    window.localStorage.setItem(key, JSON.stringify(value));
  } catch (error) {
    console.warn("Local storage is full; older conversations may be dropped.", error);
  }
}

function localId() {
  if (window.crypto?.randomUUID) {
    return `local-${window.crypto.randomUUID()}`;
  }
  return `local-${Date.now()}-${Math.random().toString(16).slice(2)}`;
}

function isUuid(value) {
  return /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i.test(
    String(value || ""),
  );
}

function byUpdated(a, b) {
  return new Date(b.updated_at || 0) - new Date(a.updated_at || 0);
}

function byCreated(a, b) {
  return new Date(b.created_at || 0) - new Date(a.created_at || 0);
}
