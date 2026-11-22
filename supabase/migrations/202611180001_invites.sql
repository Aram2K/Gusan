-- Invites (November 2026).
--
-- Every account gets a personal invite code, made on first use from the first
-- name (lower-case ASCII, at most 10 letters, "friend" without one) and 4
-- characters from abcdefghjkmnpqrstuvwxyz23456789: aram-7k2q. The link is
-- https://gusan.aibuben.xyz/i/<code>. Codes are never renamed: shared links
-- keep working. The edge function "invite" is the only caller; the functions
-- below hold every rule.
--
--   * A friend joins in the app (a hint from a recent visit of the link from the
--     same network, or a pasted link or code), at any time, once per account,
--     with a confirmed email, from a computer not seen with the inviter or with
--     another account that joined, and with an email address no earlier
--     account had. An inviter takes at most 20 joins a day.
--   * Joining is enough: the friend counts at once and gets 14 days of Pro.
--     Every 3 counted friends make one reward: 1 month of Pro, ready to claim,
--     at most 12 rewards in any 365 days.
--   * Claiming: lifetime and admin inviters cannot (not_eligible; the rewards
--     stay listed, their friends still get the gift and count). With a live
--     Stripe subscription the edge function puts one billing period on the
--     Stripe customer balance (the next bill is free) and then marks the grant
--     'credited' (invite_mark_credited). Anyone else (Free, or a manual "comp"
--     row) gets a calendar month of Pro from the end of their latest active
--     grant, or now.
--
-- Pro from invites lives in pro_grants, never in subscriptions (billing-portal
-- and my_entitlement read "the latest active subscription row"). is_pro() and
-- my_entitlement() below keep every earlier rule and add active grants;
-- my_entitlement() reports plan 'invite' when a grant is the only Pro.
--
-- Privacy: IP addresses and device ids arrive as HMAC-SHA256 hex from the
-- edge function (secret INVITE_SALT); visits are deleted after 14 days. The
-- email ledger keeps a keyed hash of the normalised address (a database-only
-- pepper), also after the account is deleted, so deleting an account and
-- signing up again does not join a second time. Nothing here is readable by
-- clients except a user's own code, invite and grants.

create extension if not exists pgcrypto with schema extensions;

-- ── the pepper for email hashes (never leaves the database) ─────────────────
create schema if not exists invite_private;
revoke all on schema invite_private from public;

create table if not exists invite_private.secret (
  id          integer primary key default 1 check (id = 1),
  pepper      text not null,
  created_at  timestamptz not null default now()
);
insert into invite_private.secret (id, pepper)
values (1, encode(sha256(convert_to(gen_random_uuid()::text || gen_random_uuid()::text
                                    || clock_timestamp()::text, 'UTF8')), 'hex'))
on conflict (id) do nothing;
revoke all on all tables in schema invite_private from public;

-- ── tables ──────────────────────────────────────────────────────────────────
create table if not exists public.invite_codes (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  code        text not null unique
              check (code ~ '^[a-z]{1,10}-[abcdefghjkmnpqrstuvwxyz23456789]{4}$'),
  created_at  timestamptz not null default now()
);
alter table public.invite_codes enable row level security;
drop policy if exists "read own invite code" on public.invite_codes;
create policy "read own invite code" on public.invite_codes
  for select using ((select auth.uid()) = user_id);

-- Pro that is not a subscription. source: invite_gift (the friend's 14 days)
-- | invite_reward (the inviter's month). status: ready (not claimed) | active
-- (starts_at..ends_at) | used (ended) | credited (a paying subscriber's month,
-- given as a Stripe customer-balance credit: stripe_balance_txn, credit_*).
create table if not exists public.pro_grants (
  id                  uuid primary key default gen_random_uuid(),
  user_id             uuid not null references auth.users(id) on delete cascade,
  source              text not null check (source in ('invite_reward', 'invite_gift')),
  status              text not null check (status in ('ready', 'active', 'used', 'credited')),
  starts_at           timestamptz,
  ends_at             timestamptz,
  created_at          timestamptz not null default now(),
  claimed_at          timestamptz,
  stripe_balance_txn  text,
  credit_amount       integer,
  credit_currency     text,
  check (status not in ('active', 'used')
         or (starts_at is not null and ends_at is not null and ends_at > starts_at)),
  check (status <> 'credited'
         or (stripe_balance_txn is not null and credit_amount > 0 and credit_currency is not null))
);
create index if not exists pro_grants_user_status_idx on public.pro_grants (user_id, status);
alter table public.pro_grants enable row level security;
drop policy if exists "read own pro grants" on public.pro_grants;
create policy "read own pro grants" on public.pro_grants
  for select using ((select auth.uid()) = user_id);

-- One row per friend who joined; a friend counts from the moment they join.
-- status 'not_counted' (with a reason) is for a join set aside by hand, for
-- abuse. inviter_id is set null (and code cleared) when the inviter deletes
-- the account; the row stays, so the friend cannot join again.
create table if not exists public.invites (
  invitee_id  uuid primary key references auth.users(id) on delete cascade,
  inviter_id  uuid references auth.users(id) on delete set null,
  code        text,
  source      text not null check (source in ('hint', 'paste')),
  joined_at   timestamptz not null default now(),
  status      text not null default 'counted' check (status in ('counted', 'not_counted')),
  counted_at  timestamptz default now(),
  reason      text check (reason is null or reason ~ '^[a-z_]{1,40}$'),
  reward_id   uuid references public.pro_grants(id) on delete set null
);
create index if not exists invites_inviter_idx on public.invites (inviter_id, joined_at desc);
alter table public.invites enable row level security;
drop policy if exists "read own invite" on public.invites;
create policy "read own invite" on public.invites
  for select using ((select auth.uid()) = invitee_id);

-- Computers (HMAC of the app's device id) seen with an account that joined an
-- invite or that opened its Invite page. Written at most once a day per row.
create table if not exists public.invite_devices (
  user_id      uuid not null references auth.users(id) on delete cascade,
  device_hmac  text not null check (device_hmac ~ '^[0-9a-f]{64}$'),
  first_seen   timestamptz not null default now(),
  last_seen    date not null default ((now() at time zone 'utc')::date),
  primary key (user_id, device_hmac)
);
create index if not exists invite_devices_hmac_idx on public.invite_devices (device_hmac);
alter table public.invite_devices enable row level security;

-- The computer each join came from, without the account: still there after
-- that account is deleted, so the same computer cannot join again.
create table if not exists public.invite_joined_devices (
  device_hmac  text primary key check (device_hmac ~ '^[0-9a-f]{64}$'),
  first_seen   timestamptz not null default now()
);
alter table public.invite_joined_devices enable row level security;

-- Visits of /i/<code> (the website worker), with the HMAC of the visitor's IP
-- address. Deleted after 14 days; hints look back 7.
create table if not exists public.invite_visits (
  id          bigint generated always as identity primary key,
  code        text not null,
  ip_hmac     text not null check (ip_hmac ~ '^[0-9a-f]{64}$'),
  created_at  timestamptz not null default now()
);
create index if not exists invite_visits_ip_idx on public.invite_visits (ip_hmac, created_at desc);
create index if not exists invite_visits_code_idx on public.invite_visits (code, created_at desc);
create index if not exists invite_visits_created_idx on public.invite_visits (created_at);
alter table public.invite_visits enable row level security;

-- Keyed hash of every normalised email address an account had: kept after the
-- account is deleted (no foreign key on purpose).
create table if not exists public.email_ledger (
  email_hmac     text primary key,
  first_user_id  uuid,
  created_at     timestamptz not null default now()
);
alter table public.email_ledger enable row level security;

-- No client writes anywhere, and no client reads of the anti-abuse tables.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke insert, update, delete, truncate on public.invite_codes, public.pro_grants,
      public.invites from anon, authenticated;
    revoke all on public.invite_devices, public.invite_joined_devices, public.invite_visits,
      public.email_ledger from anon, authenticated;
    revoke all on schema invite_private from anon, authenticated;
  end if;
end $$;

-- ── helpers (internal: no client may call them) ─────────────────────────────

-- lower-case; the last "@" splits; "+tag" removed; Gmail/Googlemail dots removed.
create or replace function public.invite_normalize_email(p_email text)
returns text
language plpgsql
immutable
set search_path = public, pg_temp
as $$
declare
  e text := lower(btrim(coalesce(p_email, '')));
  at_pos int;
  local_part text;
  domain text;
begin
  if position('@' in e) = 0 then
    return null;
  end if;
  at_pos := length(e) - position('@' in reverse(e)) + 1;
  local_part := split_part(left(e, at_pos - 1), '+', 1);
  domain := substr(e, at_pos + 1);
  if domain in ('gmail.com', 'googlemail.com') then
    local_part := replace(local_part, '.', '');
    domain := 'gmail.com';
  end if;
  if local_part = '' or domain = '' then
    return null;
  end if;
  return local_part || '@' || domain;
end;
$$;

create or replace function public.invite_email_hmac(p_email text)
returns text
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  norm text := public.invite_normalize_email(p_email);
  pep text;
begin
  if norm is null then
    return null;
  end if;
  select s.pepper into pep from invite_private.secret s where s.id = 1;
  return encode(extensions.hmac(convert_to(norm, 'UTF8'), convert_to(pep, 'UTF8'), 'sha256'), 'hex');
end;
$$;

-- First word of profiles.full_name, first letter upper-cased; null without one.
create or replace function public.invite_first_name(p_user uuid)
returns text
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select nullif(upper(left(w, 1)) || substr(w, 2), '')
  from (
    select left(split_part(regexp_replace(btrim(coalesce(p.full_name, '')), '\s+', ' ', 'g'), ' ', 1), 40) as w
    from public.profiles p
    where p.id = p_user
  ) n;
$$;

-- "José" -> "jose", "Anna-Maria" -> "annamaria", "Արամ" -> "friend".
create or replace function public.invite_slug(p_name text)
returns text
language sql
immutable
set search_path = public, pg_temp
as $$
  select coalesce(nullif(left(regexp_replace(
    translate(lower(coalesce(p_name, '')),
              'àáâãäåāăąçćčďđèéêëēėęěğìíîïīįıľłñńňòóôõöøōőŕřśşšťţùúûüūůűųýÿźżž',
              'aaaaaaaaacccddeeeeeeeegiiiiiiillnnnoooooooorrsssttuuuuuuuuyyzzz'),
    '[^a-z]', '', 'g'), 10), ''), 'friend');
$$;

-- The caller's code, made on first use and never changed. Unique; retried on a clash.
create or replace function public.invite_ensure_code(p_user uuid)
returns text
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  alphabet constant text := 'abcdefghjkmnpqrstuvwxyz23456789';
  existing text;
  base text;
  candidate text;
  raw bytea;
  tries int := 0;
begin
  select c.code into existing from public.invite_codes c where c.user_id = p_user;
  if existing is not null then
    return existing;
  end if;
  base := public.invite_slug(public.invite_first_name(p_user));
  loop
    tries := tries + 1;
    raw := uuid_send(gen_random_uuid());
    candidate := base || '-'
      || substr(alphabet, 1 + (get_byte(raw, 0) % 31), 1)
      || substr(alphabet, 1 + (get_byte(raw, 1) % 31), 1)
      || substr(alphabet, 1 + (get_byte(raw, 2) % 31), 1)
      || substr(alphabet, 1 + (get_byte(raw, 3) % 31), 1);
    begin
      insert into public.invite_codes (user_id, code) values (p_user, candidate);
      return candidate;
    exception when unique_violation then
      select c.code into existing from public.invite_codes c where c.user_id = p_user;
      if existing is not null then
        return existing;
      end if;
      if tries >= 25 then
        raise exception 'invite code: no free code for %', base;
      end if;
    end;
  end loop;
end;
$$;

-- Remember a computer for an account, at most one write a day, at most 20 per account.
create or replace function public.invite_note_device(p_user uuid, p_device_hmac text)
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if p_user is null or p_device_hmac is null or p_device_hmac !~ '^[0-9a-f]{64}$' then
    return;
  end if;
  if not exists (
       select 1 from public.invite_devices d
       where d.user_id = p_user and d.device_hmac = p_device_hmac
     )
     and (select count(*) from public.invite_devices d where d.user_id = p_user) >= 20 then
    return;
  end if;
  insert into public.invite_devices as d (user_id, device_hmac)
  values (p_user, p_device_hmac)
  on conflict (user_id, device_hmac) do update
    set last_seen = excluded.last_seen
    where d.last_seen < excluded.last_seen;
end;
$$;

-- Every 3 counted friends not yet used make one ready reward, at most 12 in
-- any 365 days. Returns how many it made.
create or replace function public.invite_make_rewards(p_inviter uuid)
returns integer
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  made int := 0;
  pending int;
  this_year int;
  new_id uuid;
begin
  if p_inviter is null then
    return 0;
  end if;
  perform pg_advisory_xact_lock(hashtext(p_inviter::text || ':invite_rewards'));
  loop
    select count(*) into pending from public.invites i
     where i.inviter_id = p_inviter and i.status = 'counted' and i.reward_id is null;
    exit when pending < 3;
    select count(*) into this_year from public.pro_grants g
     where g.user_id = p_inviter
       and g.source = 'invite_reward'
       and g.created_at > now() - interval '365 days';
    exit when this_year >= 12;
    insert into public.pro_grants (user_id, source, status)
    values (p_inviter, 'invite_reward', 'ready')
    returning id into new_id;
    update public.invites
       set reward_id = new_id
     where invitee_id in (
       select i.invitee_id from public.invites i
        where i.inviter_id = p_inviter and i.status = 'counted' and i.reward_id is null
        order by i.counted_at, i.joined_at
        limit 3
     );
    made := made + 1;
  end loop;
  return made;
end;
$$;

revoke all on function public.invite_normalize_email(text) from public, anon, authenticated;
revoke all on function public.invite_email_hmac(text) from public, anon, authenticated;
revoke all on function public.invite_first_name(uuid) from public, anon, authenticated;
revoke all on function public.invite_slug(text) from public, anon, authenticated;
revoke all on function public.invite_ensure_code(uuid) from public, anon, authenticated;
revoke all on function public.invite_note_device(uuid, text) from public, anon, authenticated;
revoke all on function public.invite_make_rewards(uuid) from public, anon, authenticated;

-- ── entitlement: everything as before, plus active grants ───────────────────
create or replace function public.is_pro()
returns boolean
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select
    exists (
      select 1 from public.profiles p
      where p.id = (select auth.uid()) and p.is_admin
    )
    or exists (
      select 1 from public.lifetime_purchases lp
      where lp.user_id = (select auth.uid()) and lp.status = 'granted'
    )
    or exists (
      select 1 from public.subscriptions s
      where s.user_id = (select auth.uid())
        and s.status = 'active'
        and (s.current_period_end is null or s.current_period_end > now())
    )
    or exists (
      select 1 from public.pro_grants g
      where g.user_id = (select auth.uid())
        and g.status = 'active'
        and g.starts_at <= now()
        and g.ends_at > now()
    );
$$;
revoke all on function public.is_pro() from public, anon;
grant execute on function public.is_pro() to authenticated;

-- Same columns as before. Plan 'invite' only when a grant is the caller's only
-- Pro; then current_period_end is the end of the latest active grant.
create or replace function public.my_entitlement()
returns table (
  is_pro boolean,
  plan text,
  status text,
  current_period_end timestamptz,
  cancel_at_period_end boolean,
  trial_available boolean,
  is_admin boolean,
  commitment_ends_at timestamptz,
  cancels_at timestamptz
)
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  with sub as (
    select * from public.subscriptions s
    where s.user_id = (select auth.uid())
      and s.status = 'active'
      and (s.current_period_end is null or s.current_period_end > now())
    order by s.current_period_end desc nulls last
    limit 1
  ), life as (
    select 1 as ok from public.lifetime_purchases lp
    where lp.user_id = (select auth.uid()) and lp.status = 'granted'
  ), prof as (
    select * from public.profiles p where p.id = (select auth.uid())
  ), grant_now as (
    select max(g.ends_at) as ends_at
    from public.pro_grants g
    where g.user_id = (select auth.uid())
      and g.status = 'active'
      and g.ends_at > now()
    having bool_or(g.starts_at <= now())
  ), paid as (
    select (exists (select 1 from sub)
            or exists (select 1 from life)
            or coalesce((select is_admin from prof), false)) as yes
  )
  select
    (coalesce((select is_admin from prof), false)
      or exists (select 1 from sub)
      or exists (select 1 from life)
      or exists (select 1 from grant_now)) as is_pro,
    coalesce(
      (select plan from sub),
      case when exists (select 1 from life) then 'lifetime' end,
      case when coalesce((select is_admin from prof), false) then 'admin' end,
      case when exists (select 1 from grant_now) then 'invite' end
    ) as plan,
    coalesce(
      (select status from sub),
      case when exists (select 1 from life) then 'active' end,
      case when not (select yes from paid) and exists (select 1 from grant_now) then 'active' end
    ) as status,
    case
      when exists (select 1 from sub) then (select current_period_end from sub)
      when not (select yes from paid) then (select ends_at from grant_now)
    end as current_period_end,
    coalesce((select cancel_at_period_end from sub), false) as cancel_at_period_end,
    false as trial_available,
    coalesce((select is_admin from prof), false) as is_admin,
    (select commitment_ends_at from sub) as commitment_ends_at,
    (select cancels_at from sub) as cancels_at;
$$;
revoke all on function public.my_entitlement() from public, anon;
grant execute on function public.my_entitlement() to authenticated;

-- ── what the app calls (through the edge function "invite") ─────────────────

-- The Invite page: code, friends, rewards, and the caller's own invite.
create or replace function public.invite_me(p_device_hmac text default null)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  my_code text;
  inv public.invites%rowtype;
  joined boolean;
  n_counted int;
  n_joined int;
  n_rewards_year int;
  friends jsonb;
  rewards jsonb;
  gift jsonb;
begin
  if uid is null then
    return jsonb_build_object('error', 'signed_out');
  end if;
  my_code := public.invite_ensure_code(uid);
  perform public.invite_note_device(uid, p_device_hmac);
  perform public.invite_make_rewards(uid);

  select * into inv from public.invites i where i.invitee_id = uid;
  joined := found;

  select count(*) filter (where i.status = 'counted'), count(*)
    into n_counted, n_joined
    from public.invites i
   where i.inviter_id = uid;

  select count(*) into n_rewards_year
    from public.pro_grants g
   where g.user_id = uid
     and g.source = 'invite_reward'
     and g.created_at > now() - interval '365 days';

  select coalesce(jsonb_agg(f.item order by f.joined_at desc), '[]'::jsonb) into friends
  from (
    select i.joined_at,
           jsonb_build_object(
             'name', coalesce(n.name, 'A friend'),
             'initial', coalesce(upper(left(n.name, 1)), ''),
             'status', case when i.status = 'counted' then 'counted' else 'not_counted' end,
             'joined_at', i.joined_at
           ) || case when i.status = 'counted' then '{}'::jsonb
                     else jsonb_build_object('reason', coalesce(i.reason, 'removed')) end as item
      from public.invites i
      cross join lateral (select public.invite_first_name(i.invitee_id) as name) n
     where i.inviter_id = uid
     order by i.joined_at desc
     limit 100
  ) f;

  select coalesce(jsonb_agg(jsonb_build_object(
           'id', g.id,
           'status', case when g.status = 'active' and g.ends_at <= now() then 'used' else g.status end,
           'starts_at', g.starts_at,
           'ends_at', g.ends_at
         ) order by g.created_at desc), '[]'::jsonb)
    into rewards
    from (
      select * from public.pro_grants x
       where x.user_id = uid and x.source = 'invite_reward'
       order by x.created_at desc
       limit 60
    ) g;

  select jsonb_build_object(
           'active', g.status = 'active' and g.starts_at <= now() and g.ends_at > now(),
           'ends_at', g.ends_at)
    into gift
    from public.pro_grants g
   where g.user_id = uid and g.source = 'invite_gift'
   order by g.created_at desc
   limit 1;

  return jsonb_build_object(
    'code', my_code,
    'first_name', public.invite_first_name(uid),
    'counted', n_counted,
    'joined_total', n_joined,
    'rewards_left_this_year', greatest(0, 12 - n_rewards_year),
    'friends', friends,
    'rewards', rewards,
    'gift', gift,
    'invited_by', case when joined
                       then jsonb_build_object('name', public.invite_first_name(inv.inviter_id)) end,
    'can_join', not joined
  );
end;
$$;

-- The one code visited from the caller's network in the last 7 days, if
-- exactly one, for an account that has not joined yet.
create or replace function public.invite_hint(p_ip_hmac text)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  codes text[];
  owner_id uuid;
begin
  if uid is null or p_ip_hmac is null or p_ip_hmac !~ '^[0-9a-f]{64}$' then
    return jsonb_build_object('found', false);
  end if;
  if exists (select 1 from public.invites i where i.invitee_id = uid) then
    return jsonb_build_object('found', false);
  end if;
  select array_agg(distinct v.code) into codes
    from public.invite_visits v
    join public.invite_codes c on c.code = v.code
   where v.ip_hmac = p_ip_hmac
     and v.created_at > now() - interval '7 days'
     and c.user_id <> uid;
  if codes is null or array_length(codes, 1) <> 1 then
    return jsonb_build_object('found', false);
  end if;
  select c.user_id into owner_id from public.invite_codes c where c.code = codes[1];
  return jsonb_build_object('found', true, 'code', codes[1], 'name', public.invite_first_name(owner_id));
end;
$$;

-- Join an invite, once per account, at any time. Reasons, checked in this
-- order: bad_code, own_code, already_joined, not_confirmed, same_computer,
-- seen_before, limit. The friend counts at once and gets 14 days of Pro.
create or replace function public.invite_join(p_code text, p_device_hmac text, p_source text default 'paste')
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  c text := lower(btrim(coalesce(p_code, '')));
  inviter uuid;
  me record;
  email_h text;
  my_norm text;
  gift_end timestamptz := now() + interval '14 days';
  n int;
begin
  if uid is null then
    return jsonb_build_object('ok', false, 'reason', 'signed_out');
  end if;
  if c !~ '^[a-z]{1,10}-[abcdefghjkmnpqrstuvwxyz23456789]{4}$' then
    return jsonb_build_object('ok', false, 'reason', 'bad_code');
  end if;
  select ic.user_id into inviter from public.invite_codes ic where ic.code = c;
  if inviter is null then
    return jsonb_build_object('ok', false, 'reason', 'bad_code');
  end if;
  if inviter = uid then
    return jsonb_build_object('ok', false, 'reason', 'own_code');
  end if;
  if exists (select 1 from public.invites i where i.invitee_id = uid) then
    return jsonb_build_object('ok', false, 'reason', 'already_joined');
  end if;
  select u.created_at, u.email, u.email_confirmed_at into me from auth.users u where u.id = uid;
  if not found or me.email_confirmed_at is null then
    return jsonb_build_object('ok', false, 'reason', 'not_confirmed');
  end if;
  if p_device_hmac is null or p_device_hmac !~ '^[0-9a-f]{64}$' then
    return jsonb_build_object('ok', false, 'reason', 'same_computer');
  end if;
  if exists (select 1 from public.invite_joined_devices j where j.device_hmac = p_device_hmac)
     or exists (
       select 1 from public.invite_devices d
       where d.device_hmac = p_device_hmac
         and d.user_id <> uid
         and (d.user_id = inviter
              or exists (select 1 from public.invites i where i.invitee_id = d.user_id))
     ) then
    return jsonb_build_object('ok', false, 'reason', 'same_computer');
  end if;
  email_h := public.invite_email_hmac(me.email);
  my_norm := public.invite_normalize_email(me.email);
  if (email_h is not null and exists (
        select 1 from public.email_ledger l
        where l.email_hmac = email_h and l.first_user_id is distinct from uid))
     or (my_norm is not null and exists (
        select 1 from auth.users u
        where u.id <> uid
          and u.created_at < me.created_at
          and public.invite_normalize_email(u.email) = my_norm)) then
    return jsonb_build_object('ok', false, 'reason', 'seen_before');
  end if;

  perform pg_advisory_xact_lock(hashtext(inviter::text || ':invite_join'));
  select count(*) into n from public.invites i
   where i.inviter_id = inviter and i.joined_at > now() - interval '1 day';
  if n >= 20 then
    return jsonb_build_object('ok', false, 'reason', 'limit');
  end if;

  insert into public.invites (invitee_id, inviter_id, code, source, status, counted_at)
  values (uid, inviter, c, case when p_source = 'hint' then 'hint' else 'paste' end, 'counted', now())
  on conflict (invitee_id) do nothing;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'already_joined');
  end if;
  if email_h is not null then
    insert into public.email_ledger (email_hmac, first_user_id)
    values (email_h, uid)
    on conflict (email_hmac) do nothing;
  end if;
  insert into public.invite_joined_devices (device_hmac)
  values (p_device_hmac)
  on conflict (device_hmac) do nothing;
  perform public.invite_note_device(uid, p_device_hmac);
  insert into public.pro_grants (user_id, source, status, starts_at, ends_at, claimed_at)
  values (uid, 'invite_gift', 'active', now(), gift_end, now());
  perform public.invite_make_rewards(inviter);
  return jsonb_build_object('ok', true, 'name', public.invite_first_name(inviter), 'gift_ends_at', gift_end);
end;
$$;

-- Claim a ready reward. Reasons: not_found, already_claimed, not_eligible
-- (lifetime or admin: nothing changes). With a live Stripe subscription it
-- returns reason 'stripe_credit' and that subscription's id, and changes
-- nothing: the edge function puts the credit on the Stripe balance and then
-- calls invite_mark_credited. Otherwise a calendar month of Pro from the end
-- of the latest active grant (or now).
create or replace function public.invite_claim(p_grant_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  g public.pro_grants%rowtype;
  sub_id text;
  start_at timestamptz;
begin
  if uid is null then
    return jsonb_build_object('ok', false, 'reason', 'signed_out');
  end if;
  perform pg_advisory_xact_lock(hashtext(uid::text || ':pro_grants'));
  select * into g from public.pro_grants x
   where x.id = p_grant_id and x.user_id = uid and x.source = 'invite_reward'
   for update;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'not_found');
  end if;
  if g.status <> 'ready' then
    return jsonb_build_object('ok', false, 'reason', 'already_claimed');
  end if;
  -- Lifetime and admin never pay again, so there is nothing to give: refused, the reward stays ready.
  if exists (select 1 from public.profiles p where p.id = uid and p.is_admin)
     or exists (select 1 from public.lifetime_purchases lp where lp.user_id = uid and lp.status = 'granted') then
    return jsonb_build_object('ok', false, 'reason', 'not_eligible');
  end if;
  -- A live Stripe subscription (a manual "comp" row is not one): the next bill is made free instead.
  select s.id into sub_id
    from public.subscriptions s
   where s.user_id = uid
     and s.status = 'active'
     and s.id like 'sub\_%'
     and (s.current_period_end is null or s.current_period_end > now())
   order by s.current_period_end desc nulls last
   limit 1;
  if sub_id is not null then
    return jsonb_build_object('ok', false, 'reason', 'stripe_credit', 'subscription_id', sub_id);
  end if;
  select greatest(now(), max(x.ends_at)) into start_at
    from public.pro_grants x
   where x.user_id = uid and x.status = 'active' and x.ends_at > now();
  start_at := coalesce(start_at, now());
  update public.pro_grants
     set status = 'active', starts_at = start_at, ends_at = start_at + interval '1 month', claimed_at = now()
   where id = g.id;
  return jsonb_build_object('ok', true, 'status', 'active',
                            'starts_at', start_at, 'ends_at', start_at + interval '1 month');
end;
$$;

revoke all on function public.invite_me(text) from public, anon;
revoke all on function public.invite_hint(text) from public, anon;
revoke all on function public.invite_join(text, text, text) from public, anon;
revoke all on function public.invite_claim(uuid) from public, anon;
grant execute on function public.invite_me(text) to authenticated;
grant execute on function public.invite_hint(text) to authenticated;
grant execute on function public.invite_join(text, text, text) to authenticated;
grant execute on function public.invite_claim(uuid) to authenticated;

-- ── the edge function, with the service role ────────────────────────────────

-- After Stripe accepted the balance credit: the ready reward becomes 'credited'.
-- A repeat with the same transaction is a no-op that still answers true.
create or replace function public.invite_mark_credited(p_user uuid, p_grant_id uuid, p_txn text,
                                                       p_amount integer, p_currency text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  n int;
begin
  if p_user is null or p_grant_id is null or coalesce(p_txn, '') = '' or coalesce(p_amount, 0) <= 0
     or coalesce(p_currency, '') !~ '^[a-z]{3}$' then
    return false;
  end if;
  perform pg_advisory_xact_lock(hashtext(p_user::text || ':pro_grants'));
  update public.pro_grants
     set status = 'credited', claimed_at = now(), stripe_balance_txn = p_txn,
         credit_amount = p_amount, credit_currency = p_currency
   where id = p_grant_id and user_id = p_user and source = 'invite_reward' and status = 'ready';
  get diagnostics n = row_count;
  return n > 0 or exists (
    select 1 from public.pro_grants g
    where g.id = p_grant_id and g.user_id = p_user and g.status = 'credited' and g.stripe_balance_txn = p_txn
  );
end;
$$;

-- The inviter's first name for the invite page.
create or replace function public.invite_info(p_code text)
returns jsonb
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  c text := lower(btrim(coalesce(p_code, '')));
  owner_id uuid;
begin
  if c !~ '^[a-z]{1,10}-[abcdefghjkmnpqrstuvwxyz23456789]{4}$' then
    return jsonb_build_object('valid', false);
  end if;
  select ic.user_id into owner_id from public.invite_codes ic where ic.code = c;
  if owner_id is null then
    return jsonb_build_object('valid', false);
  end if;
  return jsonb_build_object('valid', true, 'name', public.invite_first_name(owner_id));
end;
$$;

-- One visit per code and network in 6 hours, at most 500 a code a day.
create or replace function public.invite_record_visit(p_code text, p_ip_hmac text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  c text := lower(btrim(coalesce(p_code, '')));
begin
  if c !~ '^[a-z]{1,10}-[abcdefghjkmnpqrstuvwxyz23456789]{4}$'
     or p_ip_hmac is null or p_ip_hmac !~ '^[0-9a-f]{64}$' then
    return false;
  end if;
  if not exists (select 1 from public.invite_codes ic where ic.code = c) then
    return false;
  end if;
  if exists (
    select 1 from public.invite_visits v
    where v.ip_hmac = p_ip_hmac and v.code = c and v.created_at > now() - interval '6 hours'
  ) then
    return true;
  end if;
  if (select count(*) from public.invite_visits v
      where v.code = c and v.created_at > now() - interval '1 day') >= 500 then
    return false;
  end if;
  insert into public.invite_visits (code, ip_hmac) values (c, p_ip_hmac);
  return true;
end;
$$;

revoke all on function public.invite_mark_credited(uuid, uuid, text, integer, text) from public, anon, authenticated;
revoke all on function public.invite_info(text) from public, anon, authenticated;
revoke all on function public.invite_record_visit(text, text) from public, anon, authenticated;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.invite_mark_credited(uuid, uuid, text, integer, text) to service_role;
    grant execute on function public.invite_info(text) to service_role;
    grant execute on function public.invite_record_visit(text, text) to service_role;
  end if;
end $$;

-- ── daily upkeep (pg_cron) ──────────────────────────────────────────────────
create or replace function public.invite_daily()
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  r record;
begin
  delete from public.invite_visits where created_at < now() - interval '14 days';
  update public.pro_grants set status = 'used' where status = 'active' and ends_at <= now();
  -- Rewards held back by the 12-a-year cap, once a slot frees up.
  for r in
    select i.inviter_id from public.invites i
     where i.status = 'counted' and i.reward_id is null and i.inviter_id is not null
     group by i.inviter_id
    having count(*) >= 3
  loop
    perform public.invite_make_rewards(r.inviter_id);
  end loop;
end;
$$;
revoke all on function public.invite_daily() from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_extension where extname = 'pg_cron') then
    perform cron.schedule('invite-daily', '41 3 * * *', 'select public.invite_daily()');
  end if;
end $$;

-- ── deleting an account ─────────────────────────────────────────────────────
-- Everything of the user cascades (codes, grants, computers, their own
-- invite). Before the user row goes: the email's keyed hash stays in the
-- ledger, and the friends they invited keep their row without the code.
create or replace function public.invite_on_user_delete()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  h text;
begin
  begin
    h := public.invite_email_hmac(old.email);
    if h is not null then
      insert into public.email_ledger (email_hmac, first_user_id)
      values (h, old.id)
      on conflict (email_hmac) do nothing;
    end if;
    update public.invites set code = null where inviter_id = old.id;
  exception when others then
    raise warning 'invite_on_user_delete: %', sqlstate;
  end;
  return old;
end;
$$;
revoke all on function public.invite_on_user_delete() from public, anon, authenticated;

drop trigger if exists on_auth_user_deleted_invites on auth.users;
create trigger on_auth_user_deleted_invites
  before delete on auth.users
  for each row execute function public.invite_on_user_delete();

-- Every existing account's email, earliest account first.
insert into public.email_ledger (email_hmac, first_user_id, created_at)
select distinct on (x.h) x.h, x.id, x.created_at
  from (
    select public.invite_email_hmac(u.email) as h, u.id, u.created_at
      from auth.users u
     where u.email is not null
  ) x
 where x.h is not null
 order by x.h, x.created_at
on conflict (email_hmac) do nothing;
