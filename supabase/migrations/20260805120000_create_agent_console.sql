-- Agent console: conversations, messages, and saved research articles.
-- Everything is owner-scoped through RLS, mirroring the research library tables.

create table if not exists public.agent_chats (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  title text not null default 'New conversation',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.agent_messages (
  id uuid primary key default gen_random_uuid(),
  chat_id uuid not null references public.agent_chats(id) on delete cascade,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  role text not null check (role in ('user', 'assistant')),
  content text not null default '',
  tool_trace jsonb not null default '[]'::jsonb,
  artifacts jsonb not null default '[]'::jsonb,
  article jsonb,
  created_at timestamptz not null default now()
);

create table if not exists public.research_articles (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  chat_id uuid references public.agent_chats(id) on delete set null,
  title text not null,
  subtitle text,
  summary text not null default '',
  tickers text[] not null default '{}',
  article jsonb not null default '{}'::jsonb,
  markdown text not null default '',
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_agent_chats_user_updated
  on public.agent_chats (user_id, updated_at desc);

create index if not exists idx_agent_messages_chat_created
  on public.agent_messages (chat_id, created_at);

create index if not exists idx_agent_messages_user_created
  on public.agent_messages (user_id, created_at desc);

create index if not exists idx_research_articles_user_created
  on public.research_articles (user_id, created_at desc);

alter table public.agent_chats enable row level security;
alter table public.agent_messages enable row level security;
alter table public.research_articles enable row level security;

grant usage on schema public to anon, authenticated;
grant select, insert, update, delete on public.agent_chats to authenticated;
grant select, insert, update, delete on public.agent_messages to authenticated;
grant select, insert, update, delete on public.research_articles to authenticated;

revoke all privileges on table public.agent_chats from anon;
revoke all privileges on table public.agent_messages from anon;
revoke all privileges on table public.research_articles from anon;

revoke truncate, references, trigger on table public.agent_chats from authenticated;
revoke truncate, references, trigger on table public.agent_messages from authenticated;
revoke truncate, references, trigger on table public.research_articles from authenticated;

drop policy if exists "Users can read own chats" on public.agent_chats;
create policy "Users can read own chats"
  on public.agent_chats for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own chats" on public.agent_chats;
create policy "Users can create own chats"
  on public.agent_chats for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update own chats" on public.agent_chats;
create policy "Users can update own chats"
  on public.agent_chats for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own chats" on public.agent_chats;
create policy "Users can delete own chats"
  on public.agent_chats for delete
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can read own messages" on public.agent_messages;
create policy "Users can read own messages"
  on public.agent_messages for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own messages" on public.agent_messages;
create policy "Users can create own messages"
  on public.agent_messages for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own messages" on public.agent_messages;
create policy "Users can delete own messages"
  on public.agent_messages for delete
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can read own articles" on public.research_articles;
create policy "Users can read own articles"
  on public.research_articles for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own articles" on public.research_articles;
create policy "Users can create own articles"
  on public.research_articles for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update own articles" on public.research_articles;
create policy "Users can update own articles"
  on public.research_articles for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own articles" on public.research_articles;
create policy "Users can delete own articles"
  on public.research_articles for delete
  to authenticated
  using ((select auth.uid()) = user_id);
