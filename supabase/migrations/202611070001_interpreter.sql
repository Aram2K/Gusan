-- Gusan Interpreter (2.2.0): live speech-to-speech translation in calls.
-- docs/redesign/interpreter-spec.md, section 8.
--
-- interpreter_voices    the person's own cloned voice (an ElevenLabs voice id) and their consent
-- interpreter_usage     seconds interpreted per user per UTC month (the Pro allowance)
-- interpreter_sessions  one row per Start .. Stop, counted by the heartbeats
--
-- A user can READ only their own rows. Nothing is written from the app: the edge functions
-- interpreter-session and voice-clone write with the service role, and use_interpreter_seconds()
-- is executable by the service role only (a client must never be able to give itself time back).
--
-- Also: a separate daily abuse ceiling for the Interpreter's phrase translations (smart-action
-- mode "interpret", one request per phrase), so a call does not use up the Smart Action ceiling.

create table if not exists public.interpreter_voices (
  user_id        uuid primary key references auth.users(id) on delete cascade,
  provider       text not null default 'elevenlabs',
  voice_id       text not null,
  source         text not null check (source in ('meetings', 'recorded')),
  consent_at     timestamptz not null,
  consent_phrase text not null,
  created_at     timestamptz not null default now()
);

create table if not exists public.interpreter_usage (
  user_id uuid not null references auth.users(id) on delete cascade,
  month   date not null,                                   -- first day of the UTC month
  seconds integer not null default 0 check (seconds >= 0),
  primary key (user_id, month)
);

create table if not exists public.interpreter_sessions (
  id           uuid primary key default gen_random_uuid(),
  user_id      uuid not null references auth.users(id) on delete cascade,
  started_at   timestamptz not null default now(),
  last_beat_at timestamptz not null default now(),
  seconds      integer not null default 0 check (seconds >= 0),
  ended_at     timestamptz
);
create index if not exists interpreter_sessions_user_started_idx
  on public.interpreter_sessions (user_id, started_at desc);

alter table public.interpreter_voices   enable row level security;
alter table public.interpreter_usage    enable row level security;
alter table public.interpreter_sessions enable row level security;

drop policy if exists "read own interpreter voice" on public.interpreter_voices;
create policy "read own interpreter voice" on public.interpreter_voices
  for select using ((select auth.uid()) = user_id);

drop policy if exists "read own interpreter usage" on public.interpreter_usage;
create policy "read own interpreter usage" on public.interpreter_usage
  for select using ((select auth.uid()) = user_id);

drop policy if exists "read own interpreter sessions" on public.interpreter_sessions;
create policy "read own interpreter sessions" on public.interpreter_sessions
  for select using ((select auth.uid()) = user_id);

-- No write policies (RLS already refuses client writes); the grants say the same, so a policy
-- added by mistake later still can't open writes. The service role keeps its own grants.
revoke all on public.interpreter_voices, public.interpreter_usage, public.interpreter_sessions from anon;
revoke insert, update, delete, truncate
  on public.interpreter_voices, public.interpreter_usage, public.interpreter_sessions from authenticated;

-- Add p_seconds (0 = just read) to the user's current UTC month in one atomic upsert and return
-- the seconds left of p_allowance (never below 0). The allowance is a parameter because it is a
-- secret of the edge function (INTERPRETER_MONTHLY_SECONDS), changeable without a migration.
create or replace function public.use_interpreter_seconds(p_user_id uuid, p_seconds integer, p_allowance integer)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  mbucket date := date_trunc('month', now() at time zone 'utc')::date;
  add_s   integer := least(greatest(coalesce(p_seconds, 0), 0), 86400);
  used    integer;
begin
  if p_user_id is null then
    return 0;
  end if;
  insert into public.interpreter_usage as u (user_id, month, seconds)
  values (p_user_id, mbucket, add_s)
  on conflict (user_id, month)
  do update set seconds = u.seconds + excluded.seconds
  returning u.seconds into used;
  return greatest(coalesce(p_allowance, 0) - used, 0);
end;
$$;

revoke all on function public.use_interpreter_seconds(uuid, integer, integer) from public, anon, authenticated;

-- The edge functions use the service role. A database without that role (a plain Postgres)
-- still applies the migration.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.use_interpreter_seconds(uuid, integer, integer) to service_role;
  end if;
end $$;

-- ── the Interpreter's phrase translations: a daily ceiling of their own ─────
-- Same shape as use_smart_action_quota (its own column, so neither ceiling eats the other).
alter table public.cloud_usage
  add column if not exists interpret_requests integer not null default 0;

create or replace function public.use_interpret_quota(max_per_day integer)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid  uuid := (select auth.uid());
  used integer;
begin
  if uid is null then
    return false;
  end if;
  insert into public.cloud_usage (user_id, day, interpret_requests)
  values (uid, (now() at time zone 'utc')::date, 1)
  on conflict (user_id, day)
  do update set interpret_requests = public.cloud_usage.interpret_requests + 1
  returning interpret_requests into used;
  return used <= max_per_day;
end;
$$;

revoke all on function public.use_interpret_quota(integer) from public, anon;
grant execute on function public.use_interpret_quota(integer) to authenticated;
