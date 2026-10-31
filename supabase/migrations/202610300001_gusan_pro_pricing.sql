-- Gusan Pro pricing (October 2026).
--
-- Free: cloud meeting notes and Live Assistant are metered, one each per
-- calendar month (UTC). Pro (a paid subscription or a lifetime purchase) is
-- not metered here. There is no free trial: is_pro() no longer treats
-- Stripe status 'trialing' or profiles.pro_trial_ends_at as access.
--
-- The yearly price is billed monthly with a 12-month commitment. The
-- stripe-webhook stores commitment_ends_at and a subscription schedule id.
-- Daily request caps (use_cloud_quota / use_smart_action_quota) stay; they
-- are abuse ceilings. use_cloud_seconds is not a product quota and is not
-- applied to Pro.

alter table public.subscriptions
  add column if not exists commitment_ends_at timestamptz,
  add column if not exists stripe_schedule_id text,
  add column if not exists cancels_at timestamptz;

-- Lifetime purchases. status: reserved (checkout in flight) | granted | released.
-- The service role writes grants from the webhook. reserve_lifetime_spot()
-- is the only client path, and it only inserts a reservation for auth.uid().
create table if not exists public.lifetime_purchases (
  user_id              uuid primary key references auth.users(id) on delete cascade,
  checkout_session_id  text unique,
  status               text not null check (status in ('reserved', 'granted', 'released')),
  reserved_at          timestamptz not null default now(),
  expires_at           timestamptz,
  granted_at           timestamptz
);
alter table public.lifetime_purchases enable row level security;
drop policy if exists "read own lifetime purchase" on public.lifetime_purchases;
create policy "read own lifetime purchase" on public.lifetime_purchases
  for select using ((select auth.uid()) = user_id);

-- One cloud meeting note = one meeting (note_ref, the meeting folder id).
-- A retry of the same meeting in the same month does not take another note.
create table if not exists public.meeting_note_grants (
  user_id   uuid not null references auth.users(id) on delete cascade,
  month     date not null,
  note_ref  text not null,
  created_at timestamptz not null default now(),
  primary key (user_id, month, note_ref)
);
alter table public.meeting_note_grants enable row level security;
drop policy if exists "read own meeting note grants" on public.meeting_note_grants;
create policy "read own meeting note grants" on public.meeting_note_grants
  for select using ((select auth.uid()) = user_id);

-- One Live Assistant session = one Start until Stop (or until it expires).
-- expires_at lets a crashed app resume the same session; ending it spends
-- the month's allowance.
create table if not exists public.live_sessions (
  id          uuid primary key default gen_random_uuid(),
  user_id     uuid not null references auth.users(id) on delete cascade,
  month       date not null,
  started_at  timestamptz not null default now(),
  ended_at    timestamptz,
  expires_at  timestamptz not null
);
create index if not exists live_sessions_user_month_idx
  on public.live_sessions (user_id, month);
alter table public.live_sessions enable row level security;
drop policy if exists "read own live sessions" on public.live_sessions;
create policy "read own live sessions" on public.live_sessions
  for select using ((select auth.uid()) = user_id);

-- ── entitlement ─────────────────────────────────────────────────────────────
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
    );
$$;
revoke all on function public.is_pro() from public, anon;
grant execute on function public.is_pro() to authenticated;

drop function if exists public.my_entitlement();
create function public.my_entitlement()
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
  )
  select
    (coalesce((select is_admin from prof), false)
      or exists (select 1 from sub)
      or exists (select 1 from life)) as is_pro,
    coalesce(
      (select plan from sub),
      case when exists (select 1 from life) then 'lifetime' end,
      case when coalesce((select is_admin from prof), false) then 'admin' end
    ) as plan,
    coalesce(
      (select status from sub),
      case when exists (select 1 from life) then 'active' end
    ) as status,
    (select current_period_end from sub) as current_period_end,
    coalesce((select cancel_at_period_end from sub), false) as cancel_at_period_end,
    false as trial_available,
    coalesce((select is_admin from prof), false) as is_admin,
    (select commitment_ends_at from sub) as commitment_ends_at,
    (select cancels_at from sub) as cancels_at;
$$;
revoke all on function public.my_entitlement() from public, anon;
grant execute on function public.my_entitlement() to authenticated;

-- ── free cloud meeting note (1 per UTC month) ───────────────────────────────
create or replace function public.consume_meeting_note(note_ref text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  mbucket date := date_trunc('month', now() at time zone 'utc')::date;
  ref text := left(coalesce(nullif(btrim(note_ref), ''), gen_random_uuid()::text), 200);
  n int;
begin
  if uid is null then
    return false;
  end if;
  if public.is_pro() then
    return true;
  end if;
  perform pg_advisory_xact_lock(hashtext(uid::text || ':meeting_note'));
  if exists (
    select 1 from public.meeting_note_grants g
    where g.user_id = uid and g.month = mbucket and g.note_ref = ref
  ) then
    return true;
  end if;
  select count(*) into n from public.meeting_note_grants g
   where g.user_id = uid and g.month = mbucket;
  if n >= 1 then
    return false;
  end if;
  insert into public.meeting_note_grants (user_id, month, note_ref)
  values (uid, mbucket, ref)
  on conflict do nothing;
  return true;
end;
$$;
revoke all on function public.consume_meeting_note(text) from public, anon;
grant execute on function public.consume_meeting_note(text) to authenticated;

-- ── Live Assistant session (1 per UTC month) ────────────────────────────────
create or replace function public.begin_live_session()
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  mbucket date := date_trunc('month', now() at time zone 'utc')::date;
  open_id uuid;
  n int;
  new_id uuid;
begin
  if uid is null then
    return jsonb_build_object('ok', false, 'reason', 'signed_out');
  end if;
  perform pg_advisory_xact_lock(hashtext(uid::text || ':live_session'));
  if public.is_pro() then
    insert into public.live_sessions (user_id, month, expires_at)
    values (uid, mbucket, now() + interval '12 hours')
    returning id into new_id;
    return jsonb_build_object('ok', true, 'reason', 'pro', 'session_id', new_id);
  end if;
  select s.id into open_id
    from public.live_sessions s
   where s.user_id = uid
     and s.month = mbucket
     and s.ended_at is null
     and s.expires_at > now()
   order by s.started_at desc
   limit 1;
  if open_id is not null then
    return jsonb_build_object('ok', true, 'reason', 'resume', 'session_id', open_id);
  end if;
  select count(*) into n from public.live_sessions s
   where s.user_id = uid and s.month = mbucket;
  if n >= 1 then
    return jsonb_build_object('ok', false, 'reason', 'allowance_used');
  end if;
  insert into public.live_sessions (user_id, month, expires_at)
  values (uid, mbucket, now() + interval '12 hours')
  returning id into new_id;
  return jsonb_build_object('ok', true, 'reason', 'started', 'session_id', new_id);
end;
$$;
revoke all on function public.begin_live_session() from public, anon;
grant execute on function public.begin_live_session() to authenticated;

create or replace function public.end_live_session(p_session_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  n int;
begin
  if uid is null or p_session_id is null then
    return false;
  end if;
  update public.live_sessions
     set ended_at = now()
   where id = p_session_id
     and user_id = uid
     and ended_at is null;
  get diagnostics n = row_count;
  return n > 0;
end;
$$;
revoke all on function public.end_live_session(uuid) from public, anon;
grant execute on function public.end_live_session(uuid) to authenticated;

create or replace function public.live_session_open(p_session_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
begin
  if uid is null then
    return false;
  end if;
  if public.is_pro() then
    return true;
  end if;
  if p_session_id is null then
    return false;
  end if;
  return exists (
    select 1 from public.live_sessions s
    where s.id = p_session_id
      and s.user_id = uid
      and s.ended_at is null
      and s.expires_at > now()
  );
end;
$$;
revoke all on function public.live_session_open(uuid) from public, anon;
grant execute on function public.live_session_open(uuid) to authenticated;

-- ── lifetime cap (200), taken before a checkout session is created ──────────
create or replace function public.reserve_lifetime_spot(hold_minutes integer default 60)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  hold int := least(greatest(coalesce(hold_minutes, 60), 15), 120);
  granted int;
  pending int;
  mine text;
begin
  if uid is null then
    return jsonb_build_object('ok', false, 'reason', 'signed_out', 'spots_left', 0);
  end if;
  perform pg_advisory_xact_lock(hashtext('gusan_lifetime_cap'));
  update public.lifetime_purchases
     set status = 'released'
   where status = 'reserved'
     and expires_at is not null
     and expires_at <= now();
  select status into mine from public.lifetime_purchases where user_id = uid;
  select count(*) into granted from public.lifetime_purchases where status = 'granted';
  select count(*) into pending from public.lifetime_purchases
   where status = 'reserved' and user_id <> uid;
  if mine = 'granted' then
    return jsonb_build_object('ok', false, 'reason', 'already_lifetime',
                              'spots_left', greatest(0, 200 - granted - pending));
  end if;
  if mine = 'reserved' then
    update public.lifetime_purchases
       set expires_at = now() + make_interval(mins => hold),
           reserved_at = now()
     where user_id = uid;
    return jsonb_build_object('ok', true, 'reason', 'reserved',
                              'spots_left', greatest(0, 200 - granted - pending));
  end if;
  if granted + pending >= 200 then
    return jsonb_build_object('ok', false, 'reason', 'sold_out', 'spots_left', 0);
  end if;
  insert into public.lifetime_purchases (user_id, status, expires_at)
  values (uid, 'reserved', now() + make_interval(mins => hold))
  on conflict (user_id) do update
    set status = 'reserved',
        expires_at = now() + make_interval(mins => hold),
        reserved_at = now(),
        checkout_session_id = null,
        granted_at = null;
  return jsonb_build_object('ok', true, 'reason', 'reserved',
                            'spots_left', greatest(0, 200 - granted - pending - 1));
end;
$$;
revoke all on function public.reserve_lifetime_spot(integer) from public, anon;
grant execute on function public.reserve_lifetime_spot(integer) to authenticated;

-- The webhook (service role) calls this after checkout.session.completed.
create or replace function public.grant_lifetime_purchase(p_user_id uuid, p_session_id text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if p_user_id is null or coalesce(btrim(p_session_id), '') = '' then
    return false;
  end if;
  insert into public.lifetime_purchases (user_id, checkout_session_id, status, granted_at)
  values (p_user_id, p_session_id, 'granted', now())
  on conflict (user_id) do update
    set status = 'granted',
        checkout_session_id = excluded.checkout_session_id,
        granted_at = now(),
        expires_at = null;
  return true;
end;
$$;
revoke all on function public.grant_lifetime_purchase(uuid, text) from public, anon, authenticated;

create or replace function public.release_lifetime_reservation(p_session_id text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  n int;
begin
  update public.lifetime_purchases
     set status = 'released'
   where checkout_session_id = p_session_id
     and status = 'reserved';
  get diagnostics n = row_count;
  return n > 0;
end;
$$;
revoke all on function public.release_lifetime_reservation(text) from public, anon, authenticated;

-- The webhook uses the service role. A database without that role (a plain
-- Postgres) still applies the migration.
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.grant_lifetime_purchase(uuid, text) to service_role;
    grant execute on function public.release_lifetime_reservation(text) to service_role;
  end if;
end $$;

-- attach the checkout session id to the caller's open reservation (create-checkout).
create or replace function public.attach_lifetime_checkout(p_session_id text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := (select auth.uid());
  n int;
begin
  if uid is null or coalesce(btrim(p_session_id), '') = '' then
    return false;
  end if;
  update public.lifetime_purchases
     set checkout_session_id = p_session_id
   where user_id = uid
     and status = 'reserved';
  get diagnostics n = row_count;
  return n > 0;
end;
$$;
revoke all on function public.attach_lifetime_checkout(text) from public, anon;
grant execute on function public.attach_lifetime_checkout(text) to authenticated;

create or replace function public.lifetime_spots_left()
returns integer
language sql
stable
security definer
set search_path = public, pg_temp
as $$
  select greatest(0, 200 - (
    select count(*)::int from public.lifetime_purchases
    where status = 'granted'
       or (status = 'reserved' and expires_at is not null and expires_at > now())
  ));
$$;
revoke all on function public.lifetime_spots_left() from public;
grant execute on function public.lifetime_spots_left() to anon, authenticated;

-- Service role (the webhook) is allowed to grant and release. Supabase's
-- service_role bypasses RLS and can call security definer functions it owns;
-- revoke from authenticated above is what keeps a client from granting itself Pro.
