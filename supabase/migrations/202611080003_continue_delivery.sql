create table public.agent_task_continuations (
  task_id uuid not null references public.agent_tasks on delete cascade,
  message_key text not null,
  target_task_id uuid references public.agent_tasks on delete set null,
  primary key (task_id, message_key)
);
alter table public.agent_task_continuations enable row level security;
revoke all on public.agent_task_continuations from public, anon, authenticated;
grant all on public.agent_task_continuations to service_role;

create or replace function public.continue_agent_task(p_user_id uuid, p_task_id uuid, p_message text, p_marker text, p_operation text, p_client_message_id text)
returns public.agent_tasks
language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  task public.agent_tasks;
  run public.agent_task_runs;
  previous public.agent_tasks;
  target public.agent_tasks;
  dedupe_key text;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  select * into task from public.agent_tasks where id = p_task_id and user_id = p_user_id for update;
  if not found then raise exception using errcode = 'P0004', message = 'not_found'; end if;
  if p_message is null or length(btrim(p_message)) not between 1 and 4000 or p_marker is null or
     (p_client_message_id is not null and p_client_message_id !~ '^[A-Za-z0-9_-]{1,64}$') then
    raise exception 'invalid_continuation';
  end if;
  dedupe_key := case when p_client_message_id is null then 'message:' || md5(p_message) else 'id:' || p_client_message_id end;
  select * into run from public.agent_task_runs where task_id = task.id for update;
  if not found then raise exception 'missing_agent_run'; end if;
  if p_operation in ('resume', 'begin') then
    select t.* into target from public.agent_task_continuations c
      join public.agent_tasks t on t.id = c.target_task_id and t.user_id = p_user_id
      join public.agent_task_runs r on r.task_id = t.id
      where c.task_id = p_task_id and c.message_key = dedupe_key and
        (p_client_message_id is not null or (t.status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login') and r.plan->>'instruction' = p_message));
    if found then return target; end if;
    if p_client_message_id is not null and exists (
      select 1 from public.agent_task_continuations c where c.task_id = p_task_id and c.message_key = dedupe_key
    ) then raise exception using errcode = 'P0004', message = 'not_found'; end if;
    if task.status not in ('completed', 'failed', 'cancelled', 'timed_out') then
      raise exception using errcode = 'P0003', message = 'task_active';
    end if;
    if p_operation = 'resume' and (task.mode <> 'research' or task.status not in ('completed', 'failed') or
       task.finished_at is null or task.finished_at <= now() - interval '24 hours' or
       run.h_session_id is null or coalesce(run.plan->>'session_stopped', 'false') = 'true') then
      raise exception using errcode = 'P0005', message = 'session_unavailable';
    end if;
    if (select count(*) from public.agent_tasks where user_id = p_user_id and deadline_at > now() and status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login')) >= 2 then
      raise exception using errcode = 'P0001', message = 'too_many_active';
    end if;
    if (select count(*) from public.agent_task_starts where user_id = p_user_id and started_at >= now() - interval '24 hours') >= 40 then
      raise exception using errcode = 'P0002', message = 'quota_exceeded';
    end if;
    insert into public.agent_task_starts (user_id) values (p_user_id);
    if p_operation = 'resume' then
      update public.agent_task_runs set plan = jsonb_build_object(
        'instruction', btrim(p_message), 'timezone', run.plan->'timezone',
        'prepare_retry', 'creating', 'prepare_retry_kind', 'continue',
        'prepare_retry_marker', p_marker, 'prepare_retry_from_index', task.event_index,
        'continuation_claim', p_marker, 'client_message_id', p_client_message_id,
        'continuation_previous_task', to_jsonb(task), 'continuation_previous_plan', run.plan
      ) where task_id = task.id;
      update public.agent_tasks set status = 'running', steps = '[]', result = null,
        outcome = null, error_code = null, finished_at = null, confirmation = null,
        seen_at = null, confirmation_at = null, deadline_at = now() + interval '480 seconds', updated_at = now()
        where id = task.id returning * into target;
    else
      insert into public.agent_tasks (user_id, mode, title, deadline_at, seen_at)
        values (p_user_id, task.mode, left(btrim(p_message), 120), now() + interval '480 seconds', null) returning * into target;
      insert into public.agent_task_runs (task_id, plan) values (target.id, jsonb_build_object('instruction', btrim(p_message), 'continuation_claim', p_marker));
    end if;
    insert into public.agent_task_continuations as c (task_id, message_key, target_task_id)
      values (p_task_id, dedupe_key, target.id)
      on conflict (task_id, message_key) do update set target_task_id = excluded.target_task_id;
    return target;
  end if;
  if p_operation not in ('fallback', 'restore') or
     run.plan->>'prepare_retry_marker' is distinct from p_marker or
     run.plan->>'prepare_retry_kind' is distinct from 'continue' then
    raise exception 'invalid_continuation_claim';
  end if;
  if task.status <> 'running' then return task; end if;
  select * into previous from jsonb_populate_record(null::public.agent_tasks, run.plan->'continuation_previous_task');
  if previous.id is distinct from task.id then raise exception 'missing_continuation_snapshot'; end if;
  update public.agent_tasks set status = previous.status, steps = previous.steps,
    result = previous.result, outcome = previous.outcome, error_code = previous.error_code,
    seen_at = previous.seen_at, finished_at = previous.finished_at, confirmation = previous.confirmation,
    confirmation_at = previous.confirmation_at, deadline_at = previous.deadline_at, updated_at = now()
    where id = task.id returning * into task;
  update public.agent_task_runs set plan = run.plan->'continuation_previous_plan',
    h_session_id = case when p_operation = 'fallback' then null else run.h_session_id end
    where task_id = task.id;
  if p_operation = 'restore' then return task; end if;
  -- A gone-session fallback replaces the reserved slot without another charge.
  if (select count(*) from public.agent_tasks where user_id = p_user_id and deadline_at > now() and status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login')) >= 2 then
    raise exception using errcode = 'P0001', message = 'too_many_active';
  end if;
  insert into public.agent_tasks (user_id, mode, title, deadline_at, seen_at)
    values (p_user_id, task.mode, left(btrim(p_message), 120), now() + interval '480 seconds', null) returning * into target;
  insert into public.agent_task_runs (task_id, plan) values (target.id, jsonb_build_object('instruction', btrim(p_message), 'continuation_claim', p_marker));
  update public.agent_task_continuations c set target_task_id = target.id where c.task_id = p_task_id and c.message_key = dedupe_key;
  return target;
end;
$$;
revoke all on function public.continue_agent_task(uuid, uuid, text, text, text, text) from public, anon, authenticated;
grant execute on function public.continue_agent_task(uuid, uuid, text, text, text, text) to service_role;
