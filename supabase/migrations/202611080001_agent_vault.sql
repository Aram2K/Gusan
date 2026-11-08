create table public.vault_logins (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  hostname text not null,
  username_hint text check (length(username_hint) <= 64),
  has_totp boolean not null default false,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  unique (user_id, hostname)
);
alter table public.vault_logins enable row level security;
create policy vault_logins_owner_select on public.vault_logins for select to authenticated using ((select auth.uid()) = user_id);
revoke all on public.vault_logins from public, anon, authenticated;
grant select on public.vault_logins to authenticated;
grant all on public.vault_logins to service_role;

create table public.vault_events (
  id bigint generated always as identity primary key,
  user_id uuid not null references auth.users on delete cascade,
  login_id uuid,
  task_id uuid,
  event text not null check (event in ('added', 'release_requested', 'released', 'denied', 'expired', 'deleted', 'vault_created', 'vault_deleted', 'vault_delete_failed')),
  hostname text not null,
  at timestamptz not null default now()
);
alter table public.vault_events enable row level security;
create policy vault_events_owner_select on public.vault_events for select to authenticated using ((select auth.uid()) = user_id);
revoke all on public.vault_events from public, anon, authenticated, service_role;
grant select on public.vault_events to authenticated;
grant insert on public.vault_events to service_role;
grant usage on sequence public.vault_events_id_seq to service_role;

alter table public.agent_task_runs
  add column h_vault_id text,
  add column release_nonce uuid,
  add column release_nonce_expires_at timestamptz,
  add column release_count integer not null default 0;
alter table public.agent_tasks
  drop constraint agent_tasks_status_check,
  add constraint agent_tasks_status_check check (status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login', 'completed', 'failed', 'cancelled', 'timed_out')),
  add column login_request jsonb;

create or replace function public.begin_agent_task(p_user_id uuid, p_mode text, p_title text, p_max_active integer, p_max_per_day integer, p_deadline_s integer)
returns public.agent_tasks
language plpgsql security definer set search_path = public, pg_temp
as $$
declare task public.agent_tasks;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  if (select count(*) from public.agent_tasks where user_id = p_user_id and deadline_at > now() and status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login')) >= p_max_active then
    raise exception using errcode = 'P0001', message = 'too_many_active';
  end if;
  if (select count(*) from public.agent_task_starts where user_id = p_user_id and started_at >= now() - interval '24 hours') >= p_max_per_day then
    raise exception using errcode = 'P0002', message = 'quota_exceeded';
  end if;
  insert into public.agent_tasks (user_id, mode, title, deadline_at)
    values (p_user_id, p_mode, left(p_title, 120), now() + make_interval(secs => p_deadline_s)) returning * into task;
  insert into public.agent_task_runs (task_id) values (task.id);
  insert into public.agent_task_starts (user_id) values (p_user_id);
  return task;
end;
$$;
