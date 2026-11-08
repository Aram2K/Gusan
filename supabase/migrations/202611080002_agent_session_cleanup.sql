alter table public.agent_task_runs
  add column session_cleanup_pending boolean not null default false;

create table public.agent_session_cleanups (
  h_session_id text primary key,
  created_at timestamptz default now()
);
alter table public.agent_session_cleanups enable row level security;
revoke all on public.agent_session_cleanups from public, anon, authenticated;
grant all on public.agent_session_cleanups to service_role;

create function public.preserve_pending_session_cleanup()
returns trigger language plpgsql security definer set search_path = ''
as $$
begin
  if old.session_cleanup_pending and old.h_session_id is not null then
    insert into public.agent_session_cleanups (h_session_id)
      values (old.h_session_id) on conflict do nothing;
  end if;
  return old;
end;
$$;
revoke all on function public.preserve_pending_session_cleanup() from public, anon, authenticated, service_role;
create trigger preserve_pending_session_cleanup before delete on public.agent_task_runs
for each row execute function public.preserve_pending_session_cleanup();
