revoke all privileges on table public.research_runs from anon;
revoke all privileges on table public.saved_watchlists from anon;

revoke truncate, references, trigger on table public.research_runs from authenticated;
revoke truncate, references, trigger on table public.saved_watchlists from authenticated;

revoke truncate, references, trigger on table public.research_runs from service_role;
revoke truncate, references, trigger on table public.saved_watchlists from service_role;

grant select, insert, update, delete on table public.research_runs to authenticated;
grant select, insert, update, delete on table public.saved_watchlists to authenticated;

grant select, insert, update, delete on table public.research_runs to service_role;
grant select, insert, update, delete on table public.saved_watchlists to service_role;
