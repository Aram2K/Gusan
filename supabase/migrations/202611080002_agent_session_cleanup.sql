alter table public.agent_task_runs
  add column session_cleanup_pending boolean not null default false;

create function public.agent_task_deletion_allowed(p_task_id uuid)
returns boolean language sql stable security definer set search_path = ''
as $$
  select exists (
    select 1 from public.agent_tasks
    where id = p_task_id and user_id = (select auth.uid())
  ) and not exists (
    select 1 from public.agent_task_runs
    where task_id = p_task_id and session_cleanup_pending
  );
$$;
revoke all on function public.agent_task_deletion_allowed(uuid) from public, anon, authenticated, service_role;
grant execute on function public.agent_task_deletion_allowed(uuid) to authenticated;

drop policy agent_tasks_owner_delete on public.agent_tasks;
create policy agent_tasks_owner_delete on public.agent_tasks for delete to authenticated
using (
  (select auth.uid()) = user_id
  and status in ('completed', 'failed', 'cancelled', 'timed_out')
  and public.agent_task_deletion_allowed(id)
);

create function public.preserve_pending_session_cleanup()
returns trigger language plpgsql security definer set search_path = ''
as $$
begin
  perform 1 from public.agent_task_runs
    where task_id = old.id and session_cleanup_pending for update;
  if found then
    raise exception using errcode = '55000', message = 'session_cleanup_pending';
  end if;
  return old;
end;
$$;
revoke all on function public.preserve_pending_session_cleanup() from public, anon, authenticated, service_role;
create trigger preserve_pending_session_cleanup before delete on public.agent_tasks
for each row execute function public.preserve_pending_session_cleanup();
