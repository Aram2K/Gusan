-- Reserve a restart under the same per-user lock and ledger as a new start.
create or replace function public.continue_agent_task(p_user_id uuid, p_task_id uuid, p_message text, p_marker text, p_operation text default 'resume')
returns public.agent_tasks
language plpgsql security definer set search_path = public, pg_temp
as $$
declare
  task public.agent_tasks;
  run public.agent_task_runs;
  previous public.agent_tasks;
begin
  perform pg_advisory_xact_lock(hashtextextended(p_user_id::text, 0));
  select * into task from public.agent_tasks where id = p_task_id and user_id = p_user_id for update;
  if not found then
    raise exception using errcode = 'P0004', message = 'not_found';
  end if;
  select * into run from public.agent_task_runs where task_id = task.id for update;
  if not found then
    raise exception 'missing_agent_run';
  end if;
  if p_operation = 'resume' then
    if task.status not in ('completed', 'failed', 'cancelled', 'timed_out') then
      raise exception using errcode = 'P0003', message = 'task_active';
    end if;
    if task.mode <> 'research' or task.status not in ('completed', 'failed') or
       task.finished_at is null or task.finished_at <= now() - interval '24 hours' or
       run.h_session_id is null or coalesce(run.plan->>'session_stopped', 'false') = 'true' then
      raise exception using errcode = 'P0005', message = 'session_unavailable';
    end if;
    if p_message is null or length(btrim(p_message)) not between 1 and 4000 or p_marker is null then
      raise exception 'invalid_continuation';
    end if;
    if (select count(*) from public.agent_tasks where user_id = p_user_id and deadline_at > now() and status in ('queued', 'running', 'awaiting_confirmation', 'awaiting_login')) >= 2 then
      raise exception using errcode = 'P0001', message = 'too_many_active';
    end if;
    if (select count(*) from public.agent_task_starts where user_id = p_user_id and started_at >= now() - interval '24 hours') >= 40 then
      raise exception using errcode = 'P0002', message = 'quota_exceeded';
    end if;
    insert into public.agent_task_starts (user_id) values (p_user_id);
    update public.agent_task_runs set plan = jsonb_build_object(
      'instruction', btrim(p_message), 'timezone', run.plan->'timezone',
      'prepare_retry', 'creating', 'prepare_retry_kind', 'continue',
      'prepare_retry_marker', p_marker, 'prepare_retry_from_index', task.event_index,
      'continuation_previous_task', to_jsonb(task), 'continuation_previous_plan', run.plan
    ) where task_id = task.id;
    update public.agent_tasks set status = 'running', steps = '[]', result = null,
      outcome = null, error_code = null, finished_at = null, confirmation = null,
      confirmation_at = null, deadline_at = now() + interval '480 seconds', updated_at = now()
      where id = task.id returning * into task;
    return task;
  end if;
  if p_operation not in ('fallback', 'restore') or
     run.plan->>'prepare_retry_marker' is distinct from p_marker or
     run.plan->>'prepare_retry_kind' is distinct from 'continue' then
    raise exception 'invalid_continuation_claim';
  end if;
  -- Stop/cancel wins over an in-flight /messages request.
  if task.status <> 'running' then return task; end if;
  select * into previous from jsonb_populate_record(null::public.agent_tasks, run.plan->'continuation_previous_task');
  if previous.id is distinct from task.id then raise exception 'missing_continuation_snapshot'; end if;
  update public.agent_tasks set status = previous.status, steps = previous.steps,
    result = previous.result, outcome = previous.outcome, error_code = previous.error_code,
    finished_at = previous.finished_at, confirmation = previous.confirmation,
    confirmation_at = previous.confirmation_at, deadline_at = previous.deadline_at, updated_at = now()
    where id = task.id returning * into task;
  update public.agent_task_runs set plan = run.plan->'continuation_previous_plan',
    h_session_id = case when p_operation = 'fallback' then null else run.h_session_id end
    where task_id = task.id;
  if p_operation = 'restore' then return task; end if;
  -- Replace the reserved active slot; the attempt already has one ledger entry.
  insert into public.agent_tasks (user_id, mode, title, deadline_at)
    values (p_user_id, task.mode, left(btrim(p_message), 120), now() + interval '480 seconds') returning * into task;
  insert into public.agent_task_runs (task_id) values (task.id);
  return task;
end;
$$;
revoke all on function public.continue_agent_task(uuid, uuid, text, text, text) from public, anon, authenticated;
grant execute on function public.continue_agent_task(uuid, uuid, text, text, text) to service_role;
