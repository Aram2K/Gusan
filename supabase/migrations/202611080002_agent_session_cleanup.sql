alter table public.agent_task_runs
  add column session_cleanup_pending boolean not null default false;
