alter table public.agent_tasks
  add column provider text not null default 'h' check (provider in ('h', 'devin'));
alter table public.agent_task_runs
  add column devin_session_id text,
  add column devin_session_url text,
  add column devin_api text check (devin_api in ('v1', 'v3')),
  add column devin_org_id text;

-- A follow-up on a finished task is new paid work: enforce the same caps as begin_agent_task,
-- count it as a start, and reopen the task with a fresh deadline.
create or replace function public.claim_agent_follow_up(p_user_id uuid, p_task_id uuid, p_max_active integer, p_max_per_day integer, p_deadline_s integer)
returns public.agent_tasks
language plpgsql security definer set search_path = public, pg_temp
as $$
declare task public.agent_tasks;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  select * into task from public.agent_tasks where id = p_task_id and user_id = p_user_id for update;
  if task.id is null then
    raise exception using errcode = 'P0004', message = 'not_found';
  end if;
  if not (task.status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login') and task.deadline_at > now())
     and (select count(*) from public.agent_tasks where user_id = p_user_id and deadline_at > now() and status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login')) >= p_max_active then
    raise exception using errcode = 'P0001', message = 'too_many_active';
  end if;
  if (select count(*) from public.agent_task_starts where user_id = p_user_id and started_at >= now() - interval '24 hours') >= p_max_per_day then
    raise exception using errcode = 'P0002', message = 'quota_exceeded';
  end if;
  update public.agent_tasks set status = 'running', result = null, outcome = null, error_code = null, finished_at = null, seen_at = null,
    deadline_at = now() + make_interval(secs => p_deadline_s), updated_at = now()
    where id = p_task_id returning * into task;
  insert into public.agent_task_starts (user_id) values (p_user_id);
  return task;
end;
$$;
revoke all on function public.claim_agent_follow_up(uuid, uuid, integer, integer, integer) from public, anon, authenticated;
grant execute on function public.claim_agent_follow_up(uuid, uuid, integer, integer, integer) to service_role;
