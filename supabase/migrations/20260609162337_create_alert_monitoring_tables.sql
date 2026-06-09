create table if not exists public.alert_rules (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  name text not null,
  source_url text,
  tickers text[] not null default '{}',
  active boolean not null default true,
  schedule text not null default 'daily' check (schedule in ('daily')),
  period text not null default '1y',
  max_results integer not null default 10 check (max_results between 1 and 50),
  max_alerts integer not null default 12 check (max_alerts between 1 and 50),
  volatility_threshold numeric not null default 0.55 check (volatility_threshold >= 0 and volatility_threshold <= 2),
  metadata jsonb not null default '{}'::jsonb,
  last_run_at timestamptz,
  last_run_date date,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now()
);

create table if not exists public.alert_runs (
  id uuid primary key default gen_random_uuid(),
  alert_rule_id uuid not null references public.alert_rules(id) on delete cascade,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  trigger text not null default 'manual' check (trigger in ('manual', 'scheduled')),
  status text not null default 'success' check (status in ('success', 'failed')),
  run_date date not null default current_date,
  alert_count integer not null default 0 check (alert_count >= 0),
  high_alert_count integer not null default 0 check (high_alert_count >= 0),
  digest jsonb not null default '{}'::jsonb,
  alerts jsonb not null default '[]'::jsonb,
  rows jsonb not null default '[]'::jsonb,
  payload jsonb not null default '{}'::jsonb,
  error text,
  created_at timestamptz not null default now()
);

create index if not exists idx_alert_rules_user_updated
  on public.alert_rules (user_id, updated_at desc);

create index if not exists idx_alert_rules_active_schedule
  on public.alert_rules (active, schedule, last_run_date);

create index if not exists idx_alert_runs_user_created
  on public.alert_runs (user_id, created_at desc);

create index if not exists idx_alert_runs_rule_created
  on public.alert_runs (alert_rule_id, created_at desc);

create unique index if not exists idx_alert_runs_scheduled_once_per_day
  on public.alert_runs (alert_rule_id, trigger, run_date)
  where trigger = 'scheduled';

alter table public.alert_rules enable row level security;
alter table public.alert_runs enable row level security;

revoke all privileges on table public.alert_rules from anon;
revoke all privileges on table public.alert_runs from anon;

revoke truncate, references, trigger on table public.alert_rules from authenticated;
revoke truncate, references, trigger on table public.alert_runs from authenticated;

revoke truncate, references, trigger on table public.alert_rules from service_role;
revoke truncate, references, trigger on table public.alert_runs from service_role;

grant select, insert, update, delete on table public.alert_rules to authenticated;
grant select, insert, update, delete on table public.alert_runs to authenticated;
grant select, insert, update, delete on table public.alert_rules to service_role;
grant select, insert, update, delete on table public.alert_runs to service_role;

drop policy if exists "Users can read own alert rules" on public.alert_rules;
create policy "Users can read own alert rules"
  on public.alert_rules for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own alert rules" on public.alert_rules;
create policy "Users can create own alert rules"
  on public.alert_rules for insert
  to authenticated
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can update own alert rules" on public.alert_rules;
create policy "Users can update own alert rules"
  on public.alert_rules for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check ((select auth.uid()) = user_id);

drop policy if exists "Users can delete own alert rules" on public.alert_rules;
create policy "Users can delete own alert rules"
  on public.alert_rules for delete
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can read own alert runs" on public.alert_runs;
create policy "Users can read own alert runs"
  on public.alert_runs for select
  to authenticated
  using ((select auth.uid()) = user_id);

drop policy if exists "Users can create own alert runs" on public.alert_runs;
create policy "Users can create own alert runs"
  on public.alert_runs for insert
  to authenticated
  with check (
    (select auth.uid()) = user_id
    and exists (
      select 1
      from public.alert_rules
      where alert_rules.id = alert_runs.alert_rule_id
        and alert_rules.user_id = (select auth.uid())
    )
  );

drop policy if exists "Users can update own alert runs" on public.alert_runs;
create policy "Users can update own alert runs"
  on public.alert_runs for update
  to authenticated
  using ((select auth.uid()) = user_id)
  with check (
    (select auth.uid()) = user_id
    and exists (
      select 1
      from public.alert_rules
      where alert_rules.id = alert_runs.alert_rule_id
        and alert_rules.user_id = (select auth.uid())
    )
  );

drop policy if exists "Users can delete own alert runs" on public.alert_runs;
create policy "Users can delete own alert runs"
  on public.alert_runs for delete
  to authenticated
  using ((select auth.uid()) = user_id);
