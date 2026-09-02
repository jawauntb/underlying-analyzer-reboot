-- Prism (working alias "ubermemo") storage.
--
-- prism_series_cache  shared, ticker-independent series cache keyed by
--                     (namespace, symbol, as-of month). Written only by the
--                     engine's service role; never exposed to end users.
-- prism_packets       one built packet per (ticker, as-of date), owner-scoped
--                     when a user is attached and readable to signed-in users
--                     when it is an engine-owned (user_id null) build.
-- prism_chats         memo chat turns, owner-scoped exactly like agent_messages.

create table if not exists public.prism_series_cache (
  cache_key text primary key,
  namespace text not null default 'series',
  symbol text not null,
  as_of_month text not null,
  provider text,
  entry jsonb not null default '{}'::jsonb,
  fetched_at timestamptz not null default now(),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.prism_packets (
  id uuid primary key default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  ticker text not null,
  as_of date not null default current_date,
  engine_version text not null default '1.0.0',
  recommendation text,
  conviction numeric check (conviction is null or (conviction >= 0 and conviction <= 1)),
  memo_text text not null default '',
  packet jsonb not null default '{}'::jsonb,
  build_errors jsonb not null default '[]'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.prism_chats (
  id uuid primary key default gen_random_uuid(),
  conversation_id uuid not null default gen_random_uuid(),
  user_id uuid references auth.users(id) on delete cascade,
  packet_id uuid references public.prism_packets(id) on delete set null,
  ticker text not null,
  role text not null check (role in ('user', 'assistant')),
  content text not null default '',
  citations jsonb not null default '[]'::jsonb,
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_prism_series_cache_symbol_month
  on public.prism_series_cache (symbol, as_of_month);

create index if not exists idx_prism_series_cache_namespace_month
  on public.prism_series_cache (namespace, as_of_month, fetched_at desc);

create index if not exists idx_prism_series_cache_fetched
  on public.prism_series_cache (fetched_at desc);

-- PostgREST renders `on_conflict=ticker,as_of,user_id` as a plain column list,
-- and Postgres cannot infer an expression index (coalesce(...)) from one, so an
-- expression index here makes every packet upsert fail with 42P10. `nulls not
-- distinct` (Postgres 15+, which Supabase runs) gives the same dedupe of
-- engine-owned rows (user_id is null) from a plain column list.
create unique index if not exists idx_prism_packets_ticker_as_of
  on public.prism_packets (ticker, as_of, user_id) nulls not distinct;

create index if not exists idx_prism_packets_ticker_created
  on public.prism_packets (ticker, created_at desc);

create index if not exists idx_prism_packets_user_created
  on public.prism_packets (user_id, created_at desc);

create index if not exists idx_prism_chats_conversation_created
  on public.prism_chats (conversation_id, created_at);

create index if not exists idx_prism_chats_ticker_created
  on public.prism_chats (ticker, created_at desc);

create index if not exists idx_prism_chats_user_created
  on public.prism_chats (user_id, created_at desc);

alter table public.prism_series_cache enable row level security;
alter table public.prism_packets enable row level security;
alter table public.prism_chats enable row level security;

grant usage on schema public to anon, authenticated;

-- The series cache is engine-internal: only the service role touches it.
revoke all privileges on table public.prism_series_cache from anon, authenticated;
grant select, insert, update, delete on public.prism_series_cache to service_role;

-- Packets and chats: users read and delete their own rows, the engine (service
-- role) writes them.
revoke all privileges on table public.prism_packets from anon;
revoke all privileges on table public.prism_chats from anon;

grant select, delete on public.prism_packets to authenticated;
grant select, insert, delete on public.prism_chats to authenticated;

revoke truncate, references, trigger on table public.prism_packets from authenticated;
revoke truncate, references, trigger on table public.prism_chats from authenticated;

grant select, insert, update, delete on public.prism_packets to service_role;
grant select, insert, update, delete on public.prism_chats to service_role;

drop policy if exists "Users can read own or engine packets" on public.prism_packets;
create policy "Users can read own or engine packets"
  on public.prism_packets for select
  to authenticated
  using (user_id is null or (select auth.uid()) = user_id);

drop policy if exists "Users can delete own packets" on public.prism_packets;
create policy "Users can delete own packets"
  on public.prism_packets for delete
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can read own prism chats" on public.prism_chats;
create policy "Users can read own prism chats"
  on public.prism_chats for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own prism chats" on public.prism_chats;
create policy "Users can create own prism chats"
  on public.prism_chats for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own prism chats" on public.prism_chats;
create policy "Users can delete own prism chats"
  on public.prism_chats for delete
  to authenticated
  using ((select auth.uid()) = user_id);
