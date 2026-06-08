create table if not exists public.research_runs (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  mode text not null,
  ticker text,
  title text not null,
  summary text,
  source_url text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.saved_watchlists (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name text not null,
  source_url text,
  tickers text[] not null default '{}',
  metadata jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create index if not exists idx_research_runs_user_created
  on public.research_runs (user_id, created_at desc);

create index if not exists idx_research_runs_user_mode
  on public.research_runs (user_id, mode);

create index if not exists idx_research_runs_user_ticker
  on public.research_runs (user_id, ticker);

create index if not exists idx_saved_watchlists_user_created
  on public.saved_watchlists (user_id, created_at desc);

alter table public.research_runs enable row level security;
alter table public.saved_watchlists enable row level security;

grant usage on schema public to anon, authenticated;
grant select, insert, update, delete on public.research_runs to authenticated;
grant select, insert, update, delete on public.saved_watchlists to authenticated;

drop policy if exists "Users can read own research runs" on public.research_runs;
create policy "Users can read own research runs"
  on public.research_runs for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own research runs" on public.research_runs;
create policy "Users can create own research runs"
  on public.research_runs for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update own research runs" on public.research_runs;
create policy "Users can update own research runs"
  on public.research_runs for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own research runs" on public.research_runs;
create policy "Users can delete own research runs"
  on public.research_runs for delete
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can read own watchlists" on public.saved_watchlists;
create policy "Users can read own watchlists"
  on public.saved_watchlists for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own watchlists" on public.saved_watchlists;
create policy "Users can create own watchlists"
  on public.saved_watchlists for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update own watchlists" on public.saved_watchlists;
create policy "Users can update own watchlists"
  on public.saved_watchlists for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own watchlists" on public.saved_watchlists;
create policy "Users can delete own watchlists"
  on public.saved_watchlists for delete
  to authenticated
  using ((select auth.uid()) = user_id);
