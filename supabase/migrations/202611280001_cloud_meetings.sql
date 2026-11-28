-- Gusan connector for Claude, ChatGPT and other AI assistants (docs/mcp/gusan-connector-spec.md).
--
-- cloud_meetings: the copy of a person's meetings that the app uploads when they turn on "Use your meetings
-- in AI assistants" (off until they do). Notes, what they typed and the transcript; never audio. Each row
-- belongs to one account and one computer (device_id) and is keyed by the meeting's folder name (local_id).
--
-- Who can do what:
--   * the app (the person's own sign-in: a token without a client_id claim) reads and writes its own rows;
--   * an assistant (an OAuth token from Supabase Auth's OAuth 2.1 server: it carries client_id) can only
--     read them, and only through the four cloud_meetings_* functions below (the MCP edge function calls
--     them with the assistant's token, so these rules apply to it too), plus my_entitlement (the plan);
--   * an account delete removes everything (on delete cascade).
--
-- gusan_api_pre_request() runs before every REST request (PostgREST db-pre-request) and refuses an
-- assistant's token anywhere else: other tables, other functions, inserts, deletes. The edge functions that
-- check the user themselves refuse assistant tokens in code (_shared/assistant_token.ts).

create table if not exists public.cloud_meetings (
  id uuid primary key default gen_random_uuid(),
  user_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  device_id text not null check (length(device_id) between 1 and 100),
  local_id text not null check (length(local_id) between 1 and 100),
  title text not null default '' check (length(title) <= 500),
  started_at timestamptz,
  started_local text check (started_local is null or length(started_local) <= 40), -- "2026-11-28 14:15", as the person saw it
  duration_sec integer check (duration_sec is null or duration_sec >= 0),
  language text check (language is null or length(language) <= 40),
  people jsonb not null default '[]'::jsonb
    check (jsonb_typeof(people) = 'array' and pg_column_size(people) <= 20000),
  notes_md text not null default '' check (length(notes_md) <= 400000),
  my_notes text not null default '' check (length(my_notes) <= 200000),
  transcript text not null default '' check (octet_length(transcript) <= 3000000),
  content_hash text not null default '' check (length(content_hash) <= 128),
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  -- Any language: the "simple" configuration only lowercases (no stemming, no stop words). A tsvector must
  -- stay under 1 MB, so very long transcripts are indexed on their first million characters.
  search tsvector generated always as (
    setweight(to_tsvector('simple', coalesce(title, '')), 'A')
    || setweight(to_tsvector('simple', people), 'A')
    || setweight(to_tsvector('simple', coalesce(notes_md, '') || ' ' || coalesce(my_notes, '')), 'B')
    || setweight(to_tsvector('simple', left(coalesce(transcript, ''), 1000000)), 'C')
  ) stored,
  unique (user_id, device_id, local_id)
);

create index if not exists cloud_meetings_user_started on public.cloud_meetings (user_id, started_at desc);
create index if not exists cloud_meetings_search on public.cloud_meetings using gin (search);

create or replace function public.cloud_meetings_touch()
returns trigger
language plpgsql
set search_path = ''
as $$
begin
  new.updated_at := now();
  return new;
end;
$$;

-- At most 20,000 meetings per account (a guard against a runaway upload, far above real use).
create or replace function public.cloud_meetings_cap()
returns trigger
language plpgsql
security definer
set search_path = ''
as $$
begin
  if (select count(*) from public.cloud_meetings where user_id = new.user_id) >= 20000 then
    raise exception 'cloud_meetings_full' using errcode = 'check_violation';
  end if;
  return new;
end;
$$;

drop trigger if exists cloud_meetings_touch on public.cloud_meetings;
create trigger cloud_meetings_touch before update on public.cloud_meetings
  for each row execute function public.cloud_meetings_touch();
drop trigger if exists cloud_meetings_cap on public.cloud_meetings;
create trigger cloud_meetings_cap before insert on public.cloud_meetings
  for each row execute function public.cloud_meetings_cap();

revoke all on function public.cloud_meetings_touch() from public, anon, authenticated;
revoke all on function public.cloud_meetings_cap() from public, anon, authenticated;

alter table public.cloud_meetings enable row level security;

drop policy if exists "read own cloud meetings" on public.cloud_meetings;
create policy "read own cloud meetings" on public.cloud_meetings
  for select to authenticated
  using (user_id = (select auth.uid()));

drop policy if exists "app adds own cloud meetings" on public.cloud_meetings;
create policy "app adds own cloud meetings" on public.cloud_meetings
  for insert to authenticated
  with check (user_id = (select auth.uid()) and (select auth.jwt() ->> 'client_id') is null);

drop policy if exists "app updates own cloud meetings" on public.cloud_meetings;
create policy "app updates own cloud meetings" on public.cloud_meetings
  for update to authenticated
  using (user_id = (select auth.uid()) and (select auth.jwt() ->> 'client_id') is null)
  with check (user_id = (select auth.uid()) and (select auth.jwt() ->> 'client_id') is null);

drop policy if exists "app removes own cloud meetings" on public.cloud_meetings;
create policy "app removes own cloud meetings" on public.cloud_meetings
  for delete to authenticated
  using (user_id = (select auth.uid()) and (select auth.jwt() ->> 'client_id') is null);

revoke all on table public.cloud_meetings from anon, public;
grant select, insert, update, delete on table public.cloud_meetings to authenticated;

-- ── Usage counts from the app (also only while the cloud copy is on) ──
-- One row per account and computer: {"since": "2026-11-28", "meetings": 42, "meeting_minutes": 1880,
-- "dictations": 1204, "dictation_words": 54300, "files": 17, "file_minutes": 370}. The MCP function adds the
-- computers together (service role); nobody else reads it.
create table if not exists public.app_usage_stats (
  user_id uuid not null default auth.uid() references auth.users (id) on delete cascade,
  device_id text not null check (length(device_id) between 1 and 100),
  stats jsonb not null default '{}'::jsonb check (jsonb_typeof(stats) = 'object' and pg_column_size(stats) <= 4000),
  updated_at timestamptz not null default now(),
  primary key (user_id, device_id)
);

drop trigger if exists app_usage_stats_touch on public.app_usage_stats;
create trigger app_usage_stats_touch before update on public.app_usage_stats
  for each row execute function public.cloud_meetings_touch();

alter table public.app_usage_stats enable row level security;

drop policy if exists "app manages own usage stats" on public.app_usage_stats;
create policy "app manages own usage stats" on public.app_usage_stats
  for all to authenticated
  using (user_id = (select auth.uid()) and (select auth.jwt() ->> 'client_id') is null)
  with check (user_id = (select auth.uid()) and (select auth.jwt() ->> 'client_id') is null);

revoke all on table public.app_usage_stats from anon, public;
grant select, insert, update, delete on table public.app_usage_stats to authenticated;

-- ── What an assistant can call (security invoker: the policies above apply) ──

-- The words of a search as a tsquery: every word as a prefix ("pric" finds "pricing"), all of them (any = false)
-- or any of them (any = true). At most 12 words; null when there is nothing to look for.
create or replace function public.cloud_meetings_tsquery(p_query text, p_any boolean default false)
returns tsquery
language sql
immutable
set search_path = ''
as $$
  select case when count(*) = 0 then null
    else to_tsquery('simple', string_agg(quote_literal(w) || ':*', case when p_any then ' | ' else ' & ' end))
  end
  from (
    select distinct lower(t) as w
    from regexp_split_to_table(coalesce(p_query, ''), E'[\\s&|!():*''"\\\\<>,.;?/\\[\\]{}+=~^%$#@`-]+') as t
    where length(t) > 0
    limit 12
  ) as words;
$$;

-- Meetings that match, best first, with a short excerpt around the words found (**word**). When no meeting has
-- every word, the ones with any of them.
create or replace function public.cloud_meetings_search(
  p_query text,
  p_from timestamptz default null,
  p_to timestamptz default null,
  p_limit integer default 10
)
returns table (id uuid, title text, started_at timestamptz, started_local text, duration_sec integer, people jsonb, snippet text)
language plpgsql
stable
security invoker
set search_path = ''
as $$
declare
  q tsquery := public.cloud_meetings_tsquery(p_query, false);
  n integer := least(greatest(coalesce(p_limit, 10), 1), 20);
begin
  if q is null then
    return;
  end if;
  if not exists (
    select 1 from public.cloud_meetings m
    where m.user_id = (select auth.uid()) and m.search @@ q
      and (p_from is null or m.started_at >= p_from) and (p_to is null or m.started_at < p_to)
  ) then
    q := public.cloud_meetings_tsquery(p_query, true);
  end if;
  return query
    select top.id, top.title, top.started_at, top.started_local, top.duration_sec, top.people,
      ts_headline('simple',
        left(top.notes_md || E'\n' || top.my_notes || E'\n' || top.transcript, 200000), q,
        'MaxFragments=2, MaxWords=30, MinWords=10, StartSel="**", StopSel="**", FragmentDelimiter=" … "')
    from (
      select m.id, m.title, m.started_at, m.started_local, m.duration_sec, m.people, m.notes_md, m.my_notes, m.transcript
      from public.cloud_meetings m
      where m.user_id = (select auth.uid()) and m.search @@ q
        and (p_from is null or m.started_at >= p_from) and (p_to is null or m.started_at < p_to)
      order by ts_rank(m.search, q) desc, m.started_at desc nulls last
      limit n
    ) as top;
end;
$$;

-- Newest first, optionally in a date range or with a person (part of a name, any case). total = all that match.
create or replace function public.cloud_meetings_list(
  p_from timestamptz default null,
  p_to timestamptz default null,
  p_person text default null,
  p_limit integer default 20,
  p_offset integer default 0
)
returns table (id uuid, title text, started_at timestamptz, started_local text, duration_sec integer, people jsonb, total bigint)
language sql
stable
security invoker
set search_path = ''
as $$
  select m.id, m.title, m.started_at, m.started_local, m.duration_sec, m.people, count(*) over ()
  from public.cloud_meetings m
  where m.user_id = (select auth.uid())
    and (p_from is null or m.started_at >= p_from)
    and (p_to is null or m.started_at < p_to)
    and (coalesce(p_person, '') = ''
         or m.people::text ilike '%' || replace(replace(replace(p_person, '\', '\\'), '%', '\%'), '_', '\_') || '%')
  order by m.started_at desc nulls last, m.created_at desc
  limit least(greatest(coalesce(p_limit, 20), 1), 50)
  offset greatest(coalesce(p_offset, 0), 0);
$$;

-- One meeting: everything but the transcript, and the transcript's length (read it with cloud_meetings_transcript).
create or replace function public.cloud_meetings_get(p_id uuid)
returns table (
  id uuid, title text, started_at timestamptz, started_local text, duration_sec integer, language text, people jsonb,
  notes_md text, my_notes text, transcript_chars integer
)
language sql
stable
security invoker
set search_path = ''
as $$
  select m.id, m.title, m.started_at, m.started_local, m.duration_sec, m.language, m.people, m.notes_md, m.my_notes,
    length(m.transcript)
  from public.cloud_meetings m
  where m.id = p_id and m.user_id = (select auth.uid());
$$;

-- A page of a meeting's transcript: p_chars characters (1,000 to 60,000) from p_offset (0 = the start).
create or replace function public.cloud_meetings_transcript(p_id uuid, p_offset integer default 0, p_chars integer default 30000)
returns table (part text, total integer)
language sql
stable
security invoker
set search_path = ''
as $$
  select substr(m.transcript, greatest(coalesce(p_offset, 0), 0) + 1, least(greatest(coalesce(p_chars, 30000), 1000), 60000)),
    length(m.transcript)
  from public.cloud_meetings m
  where m.id = p_id and m.user_id = (select auth.uid());
$$;

revoke all on function public.cloud_meetings_tsquery(text, boolean) from public, anon;
revoke all on function public.cloud_meetings_search(text, timestamptz, timestamptz, integer) from public, anon;
revoke all on function public.cloud_meetings_list(timestamptz, timestamptz, text, integer, integer) from public, anon;
revoke all on function public.cloud_meetings_get(uuid) from public, anon;
revoke all on function public.cloud_meetings_transcript(uuid, integer, integer) from public, anon;
grant execute on function public.cloud_meetings_tsquery(text, boolean) to authenticated;
grant execute on function public.cloud_meetings_search(text, timestamptz, timestamptz, integer) to authenticated;
grant execute on function public.cloud_meetings_list(timestamptz, timestamptz, text, integer, integer) to authenticated;
grant execute on function public.cloud_meetings_get(uuid) to authenticated;
grant execute on function public.cloud_meetings_transcript(uuid, integer, integer) to authenticated;

-- ── Assistant tokens reach nothing else over REST ──
-- Runs before every PostgREST request. A token without client_id (the app, the website, anonymous visitors)
-- passes untouched. An assistant's token may only call the four functions above; anything else is refused
-- with 403 (insufficient_privilege). my_entitlement (the plan, read only) is allowed too, for "Plan and payments".
create or replace function public.gusan_api_pre_request()
returns void
language plpgsql
stable
set search_path = ''
as $$
declare
  claims text := current_setting('request.jwt.claims', true);
begin
  if claims is null or claims = '' or (claims::jsonb ->> 'client_id') is null then
    return;
  end if;
  if coalesce(current_setting('request.path', true), '') ~ '/rpc/(cloud_meetings_(search|list|get|transcript)|my_entitlement)$' then
    return;
  end if;
  raise insufficient_privilege using message = 'An AI assistant connected to Gusan can only read Gusan meetings.';
end;
$$;

grant execute on function public.gusan_api_pre_request() to anon, authenticated, service_role;

alter role authenticator set pgrst.db_pre_request = 'public.gusan_api_pre_request';
notify pgrst, 'reload config';
