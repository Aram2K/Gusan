alter table public.agent_tasks
  add column provider text not null default 'h' check (provider in ('h', 'devin'));
alter table public.agent_task_runs
  add column devin_session_id text,
  add column devin_session_url text,
  add column devin_api text check (devin_api in ('v1', 'v3')),
  add column devin_org_id text;
