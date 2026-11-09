-- Gusan Interpreter: server fixes from the deep review (2026-11-09).
-- Apply this BEFORE deploying the interpreter-session and voice-clone versions that call it.
--
-- use_interpreter_tokens   a daily abuse ceiling on the ElevenLabs single-use tokens that
--                          interpreter-session hands out (start and token), per user
-- use_voice_preview_chars  a daily abuse ceiling on voice-clone preview (text-to-speech in the
--                          cloned voice), counted in characters (what ElevenLabs bills), per user
-- swap_interpreter_voice   saves a new cloned voice and returns the one it replaced, in one step:
--                          two clones made at the same time each get back exactly the voice they
--                          replaced, so no voice is left at ElevenLabs that no row points to
--
-- All three are for the service role only: the edge functions call them, and a client must never
-- be able to reset its own counters or set its own voice id.

alter table public.cloud_usage
  add column if not exists interpreter_tokens integer not null default 0;
alter table public.cloud_usage
  add column if not exists voice_preview_chars integer not null default 0;

-- Add p_n tokens to the user's UTC day; true while the day's total is within p_max_per_day.
create or replace function public.use_interpreter_tokens(p_user_id uuid, p_n integer, p_max_per_day integer)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  add_n integer := least(greatest(coalesce(p_n, 0), 0), 100);
  used  integer;
begin
  if p_user_id is null then
    return false;
  end if;
  insert into public.cloud_usage as u (user_id, day, interpreter_tokens)
  values (p_user_id, (now() at time zone 'utc')::date, add_n)
  on conflict (user_id, day)
  do update set interpreter_tokens = u.interpreter_tokens + excluded.interpreter_tokens
  returning u.interpreter_tokens into used;
  return used <= coalesce(p_max_per_day, 0);
end;
$$;

-- Add p_chars preview characters to the user's UTC day; true while within p_max_per_day.
create or replace function public.use_voice_preview_chars(p_user_id uuid, p_chars integer, p_max_per_day integer)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  add_c integer := least(greatest(coalesce(p_chars, 0), 0), 10000);
  used  integer;
begin
  if p_user_id is null then
    return false;
  end if;
  insert into public.cloud_usage as u (user_id, day, voice_preview_chars)
  values (p_user_id, (now() at time zone 'utc')::date, add_c)
  on conflict (user_id, day)
  do update set voice_preview_chars = u.voice_preview_chars + excluded.voice_preview_chars
  returning u.voice_preview_chars into used;
  return used <= coalesce(p_max_per_day, 0);
end;
$$;

-- Save the user's new voice (insert or replace their one row) and return the voice id it replaced
-- (null: none). One swap per user at a time: an advisory lock, because a row lock can't cover a
-- first voice (there is no row to lock yet).
create or replace function public.swap_interpreter_voice(p_user_id uuid, p_voice_id text, p_source text,
                                                        p_consent_phrase text, p_created_at timestamptz)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  made_at timestamptz := coalesce(p_created_at, now());
  prev    text;
begin
  perform pg_advisory_xact_lock(hashtextextended('interpreter_voice:' || p_user_id::text, 0));
  select voice_id into prev from public.interpreter_voices where user_id = p_user_id;
  insert into public.interpreter_voices
    (user_id, provider, voice_id, source, consent_at, consent_phrase, created_at)
  values (p_user_id, 'elevenlabs', p_voice_id, p_source, made_at, p_consent_phrase, made_at)
  on conflict (user_id) do update
    set provider = excluded.provider, voice_id = excluded.voice_id, source = excluded.source,
        consent_at = excluded.consent_at, consent_phrase = excluded.consent_phrase,
        created_at = excluded.created_at;
  return prev;
end;
$$;

revoke all on function public.use_interpreter_tokens(uuid, integer, integer) from public, anon, authenticated;
revoke all on function public.use_voice_preview_chars(uuid, integer, integer) from public, anon, authenticated;
revoke all on function public.swap_interpreter_voice(uuid, text, text, text, timestamptz)
  from public, anon, authenticated;

-- The edge functions use the service role. A database without that role (a plain Postgres)
-- still applies the migration.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.use_interpreter_tokens(uuid, integer, integer) to service_role;
    grant execute on function public.use_voice_preview_chars(uuid, integer, integer) to service_role;
    grant execute on function public.swap_interpreter_voice(uuid, text, text, text, timestamptz) to service_role;
  end if;
end $$;
