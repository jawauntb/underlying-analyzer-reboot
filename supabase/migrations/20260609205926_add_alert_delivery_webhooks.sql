alter table public.alert_rules
  add column if not exists delivery_channel text not null default 'none'
    check (delivery_channel in ('none', 'webhook')),
  add column if not exists delivery_webhook_url text,
  add column if not exists delivery_min_severity text not null default 'any'
    check (delivery_min_severity in ('any', 'high'));

alter table public.alert_runs
  add column if not exists delivery_status text not null default 'none'
    check (delivery_status in ('none', 'success', 'failed', 'skipped')),
  add column if not exists delivery_channel text,
  add column if not exists delivered_at timestamptz;

create table if not exists public.alert_deliveries (
  id uuid primary key default gen_random_uuid(),
  alert_run_id uuid not null references public.alert_runs(id) on delete cascade,
  alert_rule_id uuid not null references public.alert_rules(id) on delete cascade,
  user_id uuid not null default auth.uid() references auth.users(id) on delete cascade,
  channel text not null check (channel in ('webhook')),
  status text not null check (status in ('success', 'failed', 'skipped')),
  destination text,
  response_status integer check (response_status is null or response_status >= 100),
  error text,
  payload jsonb not null default '{}'::jsonb,
  created_at timestamptz not null default now()
);

create index if not exists idx_alert_deliveries_user_created
  on public.alert_deliveries (user_id, created_at desc);

create index if not exists idx_alert_deliveries_run_created
  on public.alert_deliveries (alert_run_id, created_at desc);

alter table public.alert_deliveries enable row level security;

revoke all privileges on table public.alert_deliveries from anon;
revoke all privileges on table public.alert_deliveries from authenticated;
revoke truncate, references, trigger on table public.alert_deliveries from service_role;

grant select on table public.alert_deliveries to authenticated;
grant select, insert, update, delete on table public.alert_deliveries to service_role;

drop policy if exists "Users can read own alert deliveries" on public.alert_deliveries;
create policy "Users can read own alert deliveries"
  on public.alert_deliveries for select
  to authenticated
  using ((select auth.uid()) = user_id);
