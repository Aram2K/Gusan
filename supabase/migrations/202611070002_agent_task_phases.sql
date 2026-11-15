-- Two-phase execution keeps the approved plan private and research read-only.
alter table public.agent_task_runs
  add column if not exists phase text not null default 'prepare' check (phase in ('prepare', 'execute')),
  add column if not exists plan jsonb;
