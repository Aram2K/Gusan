-- Invites hardening (November 2026), on top of 202611180001_invites.sql (live),
-- plus the lifetime-payment fixes for stripe-webhook.
--
-- A. Clients can no longer call the invite functions. invite_me, invite_hint,
--    invite_join and invite_claim take the user id (p_user) and run only for
--    the service role: the edge function "invite" calls them after it checked
--    the user's token. The old signatures are dropped. None of them reads
--    auth.uid() any more. (A client calling the RPCs directly could send
--    made-up computer codes, skip the rate limits and register fake computers.)
-- B. Farming: invite_join also refuses
--      * 'disposable_email' - the email's domain, or a domain it belongs to
--        (x.mailinator.com), is on invite_private.disposable_domains;
--      * 'same_network'     - 2 joins already came from the caller's network
--        (the HMAC of the IP address the edge function sends, IPv4 whole,
--        IPv6 by its /64) in 30 days, for any inviter (so also for this one);
--      * 'limit'            - this inviter already has 2 joins in 30 days from
--        the same email domain, unless it is a big public provider
--        (invite_major_domain), or 20 joins in the last day (as before).
--    Email aliases are folded before comparing (invite_normalize_email):
--    "+tag" everywhere; Gmail dots; Yahoo "-keyword"; Proton ignores . - _;
--    Fastmail "anything@user.fastmail.com" is user@fastmail.com.
-- C. "An earlier account had this email": public.account_emails (one row per
--    account, the keyed hash of its normalised email and whether it is
--    confirmed), kept by a trigger on auth.users, replaces the scan of every
--    user. Only confirmed accounts count, and a deleted account writes the
--    email ledger only if its email was confirmed.
-- D. Fail closed: an email that can't be normalised or hashed is 'seen_before'.
-- E. Stripe credit, at most once: invite_claim moves a paying subscriber's
--    reward to 'crediting' under the lock (a second claim gets
--    already_claimed); invite_mark_credited needs 'crediting';
--    invite_revert_crediting puts it back to 'ready' when Stripe fails. A
--    reward stuck in 'crediting' for a day is logged by invite_daily (manual).
-- F. Lifetime: the webhook keeps the payment intent (grant_lifetime_purchase
--    gets p_payment_intent) and revoke_lifetime_purchase releases a lifetime
--    purchase that was refunded in full or disputed.
--
-- Join reasons now, in order: bad_code, own_code, already_joined,
-- not_confirmed, disposable_email, same_computer, seen_before, same_network,
-- limit.

-- ── columns ─────────────────────────────────────────────────────────────────
alter table public.invites
  add column if not exists ip_hmac text check (ip_hmac is null or ip_hmac ~ '^[0-9a-f]{64}$'),
  add column if not exists email_domain_hmac text check (email_domain_hmac is null or email_domain_hmac ~ '^[0-9a-f]{64}$');
create index if not exists invites_network_idx
  on public.invites (ip_hmac, joined_at desc) where ip_hmac is not null;
create index if not exists invites_inviter_domain_idx
  on public.invites (inviter_id, email_domain_hmac, joined_at desc) where email_domain_hmac is not null;

alter table public.pro_grants add column if not exists crediting_at timestamptz;
alter table public.pro_grants drop constraint if exists pro_grants_status_check;
alter table public.pro_grants add constraint pro_grants_status_check
  check (status in ('ready', 'active', 'used', 'credited', 'crediting'));

alter table public.lifetime_purchases add column if not exists payment_intent_id text;
create index if not exists lifetime_purchases_payment_intent_idx
  on public.lifetime_purchases (payment_intent_id) where payment_intent_id is not null;

-- ── throwaway email services ────────────────────────────────────────────────
create table if not exists invite_private.disposable_domains (
  domain  text primary key check (domain = lower(domain) and domain ~ '^[a-z0-9.-]+\.[a-z]{2,}$')
);
revoke all on invite_private.disposable_domains from public;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on invite_private.disposable_domains from anon, authenticated;
  end if;
end $$;

insert into invite_private.disposable_domains (domain) values
  ('mailinator.com'), ('mailinator.net'), ('mailinator2.com'), ('mailinater.com'),
  ('guerrillamail.com'), ('guerrillamail.net'), ('guerrillamail.org'), ('guerrillamail.biz'),
  ('guerrillamail.de'), ('guerrillamail.info'), ('guerrillamailblock.com'), ('grr.la'),
  ('pokemail.net'), ('spam4.me'), ('sharklasers.com'),
  ('10minutemail.com'), ('10minutemail.net'), ('10minutemail.co.uk'), ('10minutemail.de'),
  ('10minemail.com'), ('20minutemail.com'), ('minuteinbox.com'),
  ('temp-mail.org'), ('temp-mail.io'), ('tempmail.com'), ('tempmail.net'), ('tempmail.dev'),
  ('tempmail.plus'), ('tempmailo.com'), ('tempmailaddress.com'), ('tempinbox.com'), ('tempail.com'),
  ('tempr.email'), ('tmpmail.org'), ('tmpmail.net'), ('tmail.ws'),
  ('yopmail.com'), ('yopmail.fr'), ('yopmail.net'), ('jetable.org'),
  ('trashmail.com'), ('trashmail.net'), ('trashmail.de'), ('trashmail.me'), ('trashmail.io'),
  ('trashmail.at'), ('trashmail.ws'), ('trash-mail.com'),
  ('wegwerfmail.de'), ('wegwerfmail.net'), ('wegwerfemail.de'),
  ('getnada.com'), ('nada.email'), ('dispostable.com'), ('maildrop.cc'), ('throwawaymail.com'),
  ('mohmal.com'), ('emailondeck.com'), ('fakeinbox.com'), ('fakemail.net'), ('emailfake.com'),
  ('generator.email'), ('mintemail.com'), ('mytemp.email'), ('mailnesia.com'), ('mailcatch.com'),
  ('mailsac.com'), ('spambox.us'), ('spamgourmet.com'), ('mailnull.com'), ('discard.email'),
  ('discardmail.com'), ('discardmail.de'), ('burnermail.io'), ('inboxkitten.com'), ('moakt.com'),
  ('mailpoof.com'), ('dropmail.me'), ('10mail.org'), ('harakirimail.com'), ('spamdecoy.net'),
  ('byom.de'), ('einrot.com'), ('getairmail.com'), ('mailexpire.com'), ('mailforspam.com'),
  ('incognitomail.org'), ('deadaddress.com'), ('spamfree24.org'), ('0-mail.com')
on conflict (domain) do nothing;

-- ── email helpers ───────────────────────────────────────────────────────────

-- The address as one person's: lower-case; split at the last "@"; then
--   everywhere:                 "+tag" removed (Outlook, Hotmail, Fastmail, ... use it);
--   gmail.com/googlemail.com:   dots removed, googlemail.com -> gmail.com;
--   yahoo.*, ymail.com, rocketmail.com: "-keyword" removed (Yahoo's throwaway aliases);
--   proton.me, protonmail.com/.ch, pm.me: ". - _" removed, -> proton.me;
--   anything@user.fastmail.com: user@fastmail.com.
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
  elsif domain ~ '^yahoo\.[a-z.]+$' or domain in ('ymail.com', 'rocketmail.com') then
    local_part := split_part(local_part, '-', 1);
  elsif domain in ('proton.me', 'protonmail.com', 'protonmail.ch', 'pm.me') then
    local_part := regexp_replace(local_part, '[._-]', '', 'g');
    domain := 'proton.me';
  elsif domain ~ '^[a-z0-9-]+\.fastmail\.com$' then
    local_part := split_part(domain, '.', 1);
    domain := 'fastmail.com';
  end if;
  if local_part = '' or domain = '' then
    return null;
  end if;
  return local_part || '@' || domain;
end;
$$;

-- Big public providers: many unrelated people share them, so they never count
-- toward the per-inviter domain limit.
create or replace function public.invite_major_domain(p_domain text)
returns boolean
language sql
immutable
set search_path = public, pg_temp
as $$
  select lower(coalesce(p_domain, '')) = any (array[
           'gmail.com', 'googlemail.com', 'outlook.com', 'hotmail.com', 'live.com', 'msn.com',
           'icloud.com', 'me.com', 'mac.com', 'ymail.com', 'rocketmail.com', 'proton.me',
           'protonmail.com', 'protonmail.ch', 'pm.me', 'mail.ru', 'bk.ru', 'inbox.ru', 'list.ru',
           'ya.ru', 'rambler.ru', 'aol.com', 'zoho.com', 'fastmail.com', 'mail.com', 'web.de',
           't-online.de', 'orange.fr', 'free.fr', 'laposte.net', 'libero.it', 'qq.com', '163.com',
           '126.com', 'naver.com', 'hey.com', 'tutanota.com', 'tuta.io', 'seznam.cz', 'wp.pl',
           'o2.pl', 'interia.pl'])
      -- One family with any country ending: yahoo.co.uk, gmx.de, yandex.com, hotmail.fr ...
      or lower(coalesce(p_domain, '')) ~ '^(yahoo|gmx|yandex|hotmail|outlook|live|aol)\.[a-z]{2,3}(\.[a-z]{2})?$';
$$;

-- The domain after the last "@", and each domain it belongs to (a.b.c: a.b.c, b.c).
create or replace function public.invite_disposable_email(p_email text)
returns boolean
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  e text := lower(btrim(coalesce(p_email, '')));
  parts text[];
  i int;
begin
  if position('@' in e) = 0 then
    return false;
  end if;
  parts := string_to_array(substr(e, length(e) - position('@' in reverse(e)) + 2), '.');
  if parts is null or coalesce(array_length(parts, 1), 0) < 2 then
    return false;
  end if;
  for i in 1 .. array_length(parts, 1) - 1 loop
    if exists (
      select 1 from invite_private.disposable_domains d
      where d.domain = array_to_string(parts[i:array_length(parts, 1)], '.')
    ) then
      return true;
    end if;
  end loop;
  return false;
end;
$$;

-- Keyed hash of a normalised email's domain (the per-inviter domain limit).
create or replace function public.invite_domain_hmac(p_norm_email text)
returns text
language plpgsql
stable
security definer
set search_path = public, pg_temp
as $$
declare
  pep text;
begin
  if p_norm_email is null or position('@' in p_norm_email) = 0 then
    return null;
  end if;
  select s.pepper into pep from invite_private.secret s where s.id = 1;
  return encode(extensions.hmac(convert_to('domain:' || split_part(p_norm_email, '@', 2), 'UTF8'),
                                convert_to(pep, 'UTF8'), 'sha256'), 'hex');
end;
$$;

revoke all on function public.invite_normalize_email(text) from public, anon, authenticated;
revoke all on function public.invite_major_domain(text) from public, anon, authenticated;
revoke all on function public.invite_disposable_email(text) from public, anon, authenticated;
revoke all on function public.invite_domain_hmac(text) from public, anon, authenticated;

-- ── C: one row per account, kept by a trigger ───────────────────────────────
create table if not exists public.account_emails (
  user_id     uuid primary key references auth.users(id) on delete cascade,
  email_hmac  text,
  confirmed   boolean not null default false,
  created_at  timestamptz not null default now()
);
create index if not exists account_emails_hmac_idx on public.account_emails (email_hmac) where email_hmac is not null;
alter table public.account_emails enable row level security;
do $$
begin
  if exists (select 1 from pg_roles where rolname = 'authenticated') then
    revoke all on public.account_emails from anon, authenticated;
  end if;
end $$;

create or replace function public.invite_sync_account_email()
returns trigger
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  begin
    insert into public.account_emails (user_id, email_hmac, confirmed, created_at)
    values (new.id, public.invite_email_hmac(new.email), new.email_confirmed_at is not null,
            coalesce(new.created_at, now()))
    on conflict (user_id) do update
      set email_hmac = excluded.email_hmac, confirmed = excluded.confirmed;
  exception when others then
    -- Never in the way of a sign-up or a sign-in.
    raise warning 'invite_sync_account_email: %', sqlstate;
  end;
  return new;
end;
$$;
revoke all on function public.invite_sync_account_email() from public, anon, authenticated;

drop trigger if exists on_auth_user_email_invites on auth.users;
create trigger on_auth_user_email_invites
  after insert or update of email, email_confirmed_at on auth.users
  for each row execute function public.invite_sync_account_email();

insert into public.account_emails (user_id, email_hmac, confirmed, created_at)
select u.id, public.invite_email_hmac(u.email), u.email_confirmed_at is not null, u.created_at
  from auth.users u
on conflict (user_id) do update
  set email_hmac = excluded.email_hmac, confirmed = excluded.confirmed;

-- The ledger again with the new alias rules, confirmed accounts only (rows from
-- the first backfill stay; they only ever block, never let through).
insert into public.email_ledger (email_hmac, first_user_id, created_at)
select distinct on (x.h) x.h, x.id, x.created_at
  from (
    select public.invite_email_hmac(u.email) as h, u.id, u.created_at
      from auth.users u
     where u.email is not null and u.email_confirmed_at is not null
  ) x
 where x.h is not null
 order by x.h, x.created_at
on conflict (email_hmac) do nothing;

-- A deleted account leaves its email in the ledger only if it was confirmed
-- (an unconfirmed sign-up with someone else's address must not block them).
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
    if old.email_confirmed_at is not null then
      h := public.invite_email_hmac(old.email);
      if h is not null then
        insert into public.email_ledger (email_hmac, first_user_id)
        values (h, old.id)
        on conflict (email_hmac) do nothing;
      end if;
    end if;
    update public.invites set code = null where inviter_id = old.id;
  exception when others then
    raise warning 'invite_on_user_delete: %', sqlstate;
  end;
  return old;
end;
$$;
revoke all on function public.invite_on_user_delete() from public, anon, authenticated;

-- ── A: the four functions the app reaches, for the service role only ────────
drop function if exists public.invite_me(text);
drop function if exists public.invite_hint(text);
drop function if exists public.invite_join(text, text, text);
drop function if exists public.invite_claim(uuid);

-- The Invite page: code, friends, rewards, and the user's own invite.
create or replace function public.invite_me(p_user uuid, p_device_hmac text default null)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := p_user;
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
  if uid is null or not exists (select 1 from auth.users u where u.id = uid) then
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

-- The one code visited from the user's network in the last 7 days, if exactly
-- one, for an account that has not joined yet.
create or replace function public.invite_hint(p_user uuid, p_ip_hmac text)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := p_user;
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

-- Join an invite, once per account, at any time. The friend counts at once and
-- gets 14 days of Pro. Reasons in order: bad_code, own_code, already_joined,
-- not_confirmed, disposable_email, same_computer, seen_before, same_network, limit.
create or replace function public.invite_join(p_user uuid, p_code text, p_device_hmac text,
                                              p_source text default 'paste', p_ip_hmac text default null)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := p_user;
  c text := lower(btrim(coalesce(p_code, '')));
  ip text := case when p_ip_hmac ~ '^[0-9a-f]{64}$' then p_ip_hmac end;
  inviter uuid;
  me record;
  email_h text;
  my_norm text;
  domain_h text;
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
  if public.invite_disposable_email(me.email) then
    return jsonb_build_object('ok', false, 'reason', 'disposable_email');
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
  my_norm := public.invite_normalize_email(me.email);
  email_h := public.invite_email_hmac(me.email);
  domain_h := public.invite_domain_hmac(my_norm);
  -- Fail closed: an address that can't be folded or hashed can't be told apart.
  if my_norm is null or email_h is null or domain_h is null then
    return jsonb_build_object('ok', false, 'reason', 'seen_before');
  end if;
  if exists (
       select 1 from public.email_ledger l
       where l.email_hmac = email_h and l.first_user_id is distinct from uid)
     or exists (
       select 1 from public.account_emails a
       where a.email_hmac = email_h
         and a.user_id <> uid
         and a.confirmed
         and a.created_at < me.created_at) then
    return jsonb_build_object('ok', false, 'reason', 'seen_before');
  end if;

  -- Counted under locks (the inviter's, then the network's), so joins at the same moment can't both pass.
  perform pg_advisory_xact_lock(hashtext(inviter::text || ':invite_join'));
  if ip is not null then
    perform pg_advisory_xact_lock(hashtext('invite_network:' || ip));
    select count(*) into n from public.invites i
     where i.ip_hmac = ip and i.joined_at > now() - interval '30 days';
    if n >= 2 then
      return jsonb_build_object('ok', false, 'reason', 'same_network');
    end if;
  end if;
  if not public.invite_major_domain(split_part(my_norm, '@', 2)) then
    select count(*) into n from public.invites i
     where i.inviter_id = inviter and i.email_domain_hmac = domain_h
       and i.joined_at > now() - interval '30 days';
    if n >= 2 then
      return jsonb_build_object('ok', false, 'reason', 'limit');
    end if;
  end if;
  select count(*) into n from public.invites i
   where i.inviter_id = inviter and i.joined_at > now() - interval '1 day';
  if n >= 20 then
    return jsonb_build_object('ok', false, 'reason', 'limit');
  end if;

  insert into public.invites (invitee_id, inviter_id, code, source, status, counted_at, ip_hmac, email_domain_hmac)
  values (uid, inviter, c, case when p_source = 'hint' then 'hint' else 'paste' end, 'counted', now(),
          ip, domain_h)
  on conflict (invitee_id) do nothing;
  if not found then
    return jsonb_build_object('ok', false, 'reason', 'already_joined');
  end if;
  insert into public.email_ledger (email_hmac, first_user_id)
  values (email_h, uid)
  on conflict (email_hmac) do nothing;
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
-- (lifetime or admin: nothing changes). A live Stripe subscription: the reward
-- becomes 'crediting' here, under the lock, and the answer is reason
-- 'stripe_credit' with the subscription id; the edge function then credits
-- Stripe and calls invite_mark_credited, or invite_revert_crediting on failure.
-- Otherwise a calendar month of Pro from the end of the latest active grant.
create or replace function public.invite_claim(p_user uuid, p_grant_id uuid)
returns jsonb
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  uid uuid := p_user;
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
    update public.pro_grants set status = 'crediting', crediting_at = now() where id = g.id;
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

revoke all on function public.invite_me(uuid, text) from public, anon, authenticated;
revoke all on function public.invite_hint(uuid, text) from public, anon, authenticated;
revoke all on function public.invite_join(uuid, text, text, text, text) from public, anon, authenticated;
revoke all on function public.invite_claim(uuid, uuid) from public, anon, authenticated;

-- ── E: the Stripe credit, after Stripe answered ─────────────────────────────

-- Stripe accepted the credit: 'crediting' -> 'credited'. A repeat with the same
-- transaction answers true again.
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
   where id = p_grant_id and user_id = p_user and source = 'invite_reward' and status = 'crediting';
  get diagnostics n = row_count;
  return n > 0 or exists (
    select 1 from public.pro_grants g
    where g.id = p_grant_id and g.user_id = p_user and g.status = 'credited' and g.stripe_balance_txn = p_txn
  );
end;
$$;

-- Stripe failed and nothing was credited: the reward is ready again.
create or replace function public.invite_revert_crediting(p_user uuid, p_grant_id uuid)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  n int;
begin
  if p_user is null or p_grant_id is null then
    return false;
  end if;
  perform pg_advisory_xact_lock(hashtext(p_user::text || ':pro_grants'));
  update public.pro_grants
     set status = 'ready', crediting_at = null
   where id = p_grant_id and user_id = p_user and source = 'invite_reward' and status = 'crediting';
  get diagnostics n = row_count;
  return n > 0;
end;
$$;

revoke all on function public.invite_mark_credited(uuid, uuid, text, integer, text) from public, anon, authenticated;
revoke all on function public.invite_revert_crediting(uuid, uuid) from public, anon, authenticated;

-- ── daily upkeep ────────────────────────────────────────────────────────────
create or replace function public.invite_daily()
returns void
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  r record;
  stuck int;
begin
  delete from public.invite_visits where created_at < now() - interval '14 days';
  -- A join's network and email-domain codes are needed for 30 days, then dropped.
  update public.invites set ip_hmac = null, email_domain_hmac = null
   where (ip_hmac is not null or email_domain_hmac is not null)
     and joined_at < now() - interval '30 days';
  update public.pro_grants set status = 'used' where status = 'active' and ends_at <= now();
  -- A Stripe credit that never finished: left for a person to check in Stripe (metadata grant_id).
  select count(*) into stuck from public.pro_grants
   where status = 'crediting' and crediting_at < now() - interval '1 day';
  if stuck > 0 then
    raise warning 'invite_daily: % reward(s) stuck in crediting for over a day', stuck;
  end if;
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

-- ── F: lifetime payments (stripe-webhook) ───────────────────────────────────
drop function if exists public.grant_lifetime_purchase(uuid, text);
create or replace function public.grant_lifetime_purchase(p_user_id uuid, p_session_id text,
                                                          p_payment_intent text default null)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
begin
  if p_user_id is null or coalesce(btrim(p_session_id), '') = '' then
    return false;
  end if;
  insert into public.lifetime_purchases as lp (user_id, checkout_session_id, status, granted_at, payment_intent_id)
  values (p_user_id, p_session_id, 'granted', now(), nullif(btrim(coalesce(p_payment_intent, '')), ''))
  on conflict (user_id) do update
    set status = 'granted',
        checkout_session_id = excluded.checkout_session_id,
        granted_at = now(),
        expires_at = null,
        payment_intent_id = coalesce(excluded.payment_intent_id, lp.payment_intent_id);
  return true;
end;
$$;

-- A lifetime payment refunded in full, or disputed: no longer Pro.
create or replace function public.revoke_lifetime_purchase(p_session_id text, p_payment_intent text)
returns boolean
language plpgsql
security definer
set search_path = public, pg_temp
as $$
declare
  n int;
begin
  if coalesce(btrim(p_session_id), '') = '' and coalesce(btrim(p_payment_intent), '') = '' then
    return false;
  end if;
  update public.lifetime_purchases
     set status = 'released'
   where status = 'granted'
     and ((coalesce(btrim(p_session_id), '') <> '' and checkout_session_id = p_session_id)
          or (coalesce(btrim(p_payment_intent), '') <> '' and payment_intent_id = p_payment_intent));
  get diagnostics n = row_count;
  return n > 0;
end;
$$;

revoke all on function public.grant_lifetime_purchase(uuid, text, text) from public, anon, authenticated;
revoke all on function public.revoke_lifetime_purchase(text, text) from public, anon, authenticated;

do $$
begin
  if exists (select 1 from pg_roles where rolname = 'service_role') then
    grant execute on function public.invite_me(uuid, text) to service_role;
    grant execute on function public.invite_hint(uuid, text) to service_role;
    grant execute on function public.invite_join(uuid, text, text, text, text) to service_role;
    grant execute on function public.invite_claim(uuid, uuid) to service_role;
    grant execute on function public.invite_mark_credited(uuid, uuid, text, integer, text) to service_role;
    grant execute on function public.invite_revert_crediting(uuid, uuid) to service_role;
    grant execute on function public.grant_lifetime_purchase(uuid, text, text) to service_role;
    grant execute on function public.revoke_lifetime_purchase(text, text) to service_role;
  end if;
end $$;
