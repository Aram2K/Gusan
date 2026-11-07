-- Managed agent tasks. Clients can read safe columns and delete their own rows.
create table public.agent_tasks (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null references auth.users on delete cascade,
  mode text not null check (mode in ('research', 'do')),
  status text not null default 'queued' check (status in ('queued', 'running', 'awaiting_confirmation', 'completed', 'failed', 'cancelled', 'timed_out')),
  title text not null,
  h_session_id text unique,
  event_index integer not null default 0,
  steps jsonb not null default '[]',
  confirmation jsonb,
  confirmation_at timestamptz,
  result jsonb,
  outcome text,
  error_code text,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  finished_at timestamptz,
  deadline_at timestamptz not null,
  seen_at timestamptz
);
create index agent_tasks_user_created on public.agent_tasks (user_id, created_at desc);
alter table public.agent_tasks enable row level security;
create policy agent_tasks_owner_select on public.agent_tasks for select to authenticated using ((select auth.uid()) = user_id);
create policy agent_tasks_owner_delete on public.agent_tasks for delete to authenticated using ((select auth.uid()) = user_id);
revoke all on public.agent_tasks from anon, authenticated;
grant select (id, user_id, mode, status, title, event_index, steps, confirmation_at, result, outcome, error_code, created_at, updated_at, finished_at, deadline_at, seen_at) on public.agent_tasks to authenticated;
grant delete on public.agent_tasks to authenticated;
grant all on public.agent_tasks to service_role;

create or replace function public.begin_agent_task(p_user_id uuid, p_mode text, p_title text, p_max_active integer, p_max_per_day integer, p_deadline_s integer)
returns public.agent_tasks
language plpgsql security definer set search_path = public, pg_temp
as $$
declare task public.agent_tasks;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  if (select count(*) from public.agent_tasks where user_id = p_user_id and status in ('queued', 'running', 'awaiting_confirmation')) >= p_max_active then
    raise exception using errcode = 'P0001', message = 'too_many_active';
  end if;
  if (select count(*) from public.agent_tasks where user_id = p_user_id and created_at >= now() - interval '24 hours') >= p_max_per_day then
    raise exception using errcode = 'P0002', message = 'quota_exceeded';
  end if;
  insert into public.agent_tasks (user_id, mode, title, deadline_at)
    values (p_user_id, p_mode, left(p_title, 120), now() + make_interval(secs => p_deadline_s)) returning * into task;
  return task;
end;
$$;
revoke all on function public.begin_agent_task(uuid, text, text, integer, integer, integer) from public, anon, authenticated;
grant execute on function public.begin_agent_task(uuid, text, text, integer, integer, integer) to service_role;

create or replace function public.mark_agent_task_seen(p_task_id uuid)
returns void language sql security definer set search_path = public, pg_temp
as $$
  update public.agent_tasks set seen_at = now(), updated_at = now()
  where id = p_task_id and user_id = (select auth.uid());
$$;
revoke all on function public.mark_agent_task_seen(uuid) from public, anon;
grant execute on function public.mark_agent_task_seen(uuid) to authenticated;

do $$
begin
  if exists (select 1 from pg_publication where pubname = 'supabase_realtime')
    and not exists (select 1 from pg_publication_tables where pubname = 'supabase_realtime' and schemaname = 'public' and tablename = 'agent_tasks') then
    alter publication supabase_realtime add table public.agent_tasks;
  end if;
end;
$$;
