"""Invites, server side: the SQL migration and the edge function "invite".

There is no database in this suite, so these read the sources the way
test_pricing_policy.py does and pin the rules the founder decided (join once,
at any time; a friend counts on joining and gets 14 days of Pro; 3 friends a
month of Pro, at most 12 a year; 20 joins a day; lifetime/admin can't claim;
paying subscribers get the month as a Stripe credit on their next bill) and the
contract the app is built against (supabase/functions/invite/index.ts)."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "supabase" / "migrations"
SQL_PATH = MIGRATIONS / "202611180001_invites.sql"
HARD_PATH = MIGRATIONS / "202611200001_invites_hardening.sql"   # applied after it (live)
TS_PATH = ROOT / "supabase" / "functions" / "invite" / "index.ts"

SQL = SQL_PATH.read_text(encoding="utf-8")
HARD = HARD_PATH.read_text(encoding="utf-8")
JOIN_REASONS = ["'bad_code'", "'own_code'", "'already_joined'", "'not_confirmed'", "'disposable_email'",
                "'same_computer'", "'seen_before'", "'same_network'", "'limit'"]
TS = TS_PATH.read_text(encoding="utf-8")
ALPHABET = "abcdefghjkmnpqrstuvwxyz23456789"

USER_RPCS = {
    "invite_me": "text",
    "invite_hint": "text",
    "invite_join": "text, text, text",
    "invite_claim": "uuid",
}
SERVICE_RPCS = {
    "invite_info": "text",
    "invite_record_visit": "text, text",
    "invite_mark_credited": "uuid, uuid, text, integer, text",
}
INTERNAL = {
    "invite_normalize_email": "text",
    "invite_email_hmac": "text",
    "invite_first_name": "uuid",
    "invite_slug": "text",
    "invite_ensure_code": "uuid",
    "invite_note_device": "uuid, text",
    "invite_make_rewards": "uuid",
    "invite_daily": "",
    "invite_on_user_delete": "",
}
TABLES = ("invite_codes", "pro_grants", "invites", "invite_devices", "invite_joined_devices",
          "invite_visits", "email_ledger")


def function_body(sql, name):
    """From `create or replace function public.<name>(` to the closing `$$;`."""
    start = sql.index(f"create or replace function public.{name}(")
    open_at = sql.index("as $$", start)
    end = sql.index("$$;", open_at + 5)
    return sql[start:end + 3]


def table_body(sql, name):
    start = sql.index(f"create table if not exists public.{name} (")
    return sql[start:sql.index(");", start)]


def newest_definition(name):
    """The last migration (by file name) that (re)defines public.<name>()."""
    marker = f"create or replace function public.{name}("
    found = [p for p in sorted(MIGRATIONS.glob("*.sql")) if marker in p.read_text(encoding="utf-8")]
    return found[-1], function_body(found[-1].read_text(encoding="utf-8"), name)


class TestMigrationShape(unittest.TestCase):
    def test_the_file_names_sort_after_the_older_ones_and_their_prefixes_are_unique(self):
        # The two run one after the other, after every migration before them (later ones, like
        # 202611280001_cloud_meetings.sql, come after both).
        names = sorted(p.name for p in MIGRATIONS.glob("*.sql"))
        at = names.index(SQL_PATH.name)
        self.assertEqual(names[at:at + 2], [SQL_PATH.name, HARD_PATH.name])
        self.assertTrue(all(n < "202611180001" for n in names[:at]))
        for path in (SQL_PATH, HARD_PATH):
            prefix = path.name.split("_")[0]
            self.assertEqual([n for n in names if n.startswith(prefix + "_")], [path.name])

    def test_every_table_has_row_level_security_and_no_client_write_policy(self):
        for table in TABLES:
            self.assertIn(f"create table if not exists public.{table} (", SQL)
            self.assertIn(f"alter table public.{table} enable row level security;", SQL)
        policies = re.findall(r"create policy [^;]+;", SQL, flags=re.S)
        self.assertEqual(len(policies), 3)
        for policy in policies:
            self.assertIn("for select using ((select auth.uid()) =", policy)
            for verb in ("for insert", "for update", "for delete", "for all", "with check"):
                self.assertNotIn(verb, policy)

    def test_no_days_of_use_and_no_banked_months(self):
        for gone in ("invite_activity", "days_used", "join_until", "banked", "invite_activate_banked",
                     "invite_paid_active", "too_late"):
            self.assertNotIn(gone, SQL, gone)
        for gone in ('"activity"', "days_used", "join_until", "banked"):
            self.assertNotIn(gone, TS, gone)

    def test_clients_cannot_read_the_anti_abuse_tables(self):
        for table in ("invite_devices", "invite_joined_devices", "invite_visits", "email_ledger"):
            self.assertNotRegex(SQL, rf"create policy [^;]+ on public\.{table}\b")
        self.assertIn("revoke all on public.invite_devices, public.invite_joined_devices, "
                      "public.invite_visits,\n      public.email_ledger from anon, authenticated;", SQL)
        self.assertIn("revoke all on schema invite_private from public;", SQL)

    def test_every_function_is_definer_safe(self):
        for name in [*USER_RPCS, *SERVICE_RPCS, *INTERNAL, "is_pro", "my_entitlement"]:
            body = function_body(SQL, name)
            self.assertIn("set search_path = public, pg_temp", body, name)
            if name not in ("invite_normalize_email", "invite_slug"):
                self.assertIn("security definer", body, name)

    def test_grants(self):
        for name, args in USER_RPCS.items():
            self.assertIn(f"revoke all on function public.{name}({args}) from public, anon;", SQL)
            self.assertIn(f"grant execute on function public.{name}({args}) to authenticated;", SQL)
        for name, args in SERVICE_RPCS.items():
            self.assertIn(f"revoke all on function public.{name}({args}) from public, anon, authenticated;", SQL)
            self.assertIn(f"grant execute on function public.{name}({args}) to service_role;", SQL)
            self.assertNotIn(f"grant execute on function public.{name}({args}) to authenticated", SQL)
        for name, args in INTERNAL.items():
            self.assertIn(f"revoke all on function public.{name}({args}) from public, anon, authenticated;", SQL)
            self.assertNotRegex(SQL, rf"grant execute on function public\.{name}\(")

    def test_grants_never_go_into_subscriptions(self):
        self.assertNotRegex(SQL, r"(insert into|update|delete from)\s+public\.subscriptions")

    def test_deleting_an_account_cascades_but_keeps_the_email_ledger(self):
        for table in ("invite_codes", "pro_grants", "invite_devices"):
            self.assertIn("references auth.users(id) on delete cascade", table_body(SQL, table), table)
        invites = table_body(SQL, "invites")
        self.assertIn("invitee_id  uuid primary key references auth.users(id) on delete cascade", invites)
        # The friend keeps a row when the inviter goes, so they cannot join a second time.
        self.assertIn("inviter_id  uuid references auth.users(id) on delete set null", invites)
        self.assertNotIn("references", table_body(SQL, "email_ledger"))
        self.assertNotIn("references", table_body(SQL, "invite_joined_devices"))
        trigger = function_body(SQL, "invite_on_user_delete")
        self.assertIn("insert into public.email_ledger", trigger)
        self.assertIn("exception when others", trigger)  # never blocks a deletion
        self.assertIn("before delete on auth.users", SQL)

    def test_existing_accounts_are_backfilled_into_the_ledger_earliest_first(self):
        tail = SQL[SQL.rindex("insert into public.email_ledger (email_hmac, first_user_id, created_at)"):]
        self.assertIn("from auth.users u", tail)
        self.assertIn("distinct on (x.h)", tail)
        self.assertIn("order by x.h, x.created_at", tail)


class TestRules(unittest.TestCase):
    def test_codes(self):
        self.assertIn(f"'{ALPHABET}'", function_body(SQL, "invite_ensure_code"))
        self.assertIn(f"^[a-z]{{1,10}}-[{ALPHABET}]{{4}}$", SQL)
        self.assertIn(f"[{ALPHABET}]{{4}}", TS)
        slug = function_body(SQL, "invite_slug")
        self.assertIn("'friend'", slug)
        self.assertIn(", 10)", slug)
        self.assertIn("'[^a-z]'", slug)
        translate = re.search(r"translate\(lower\(coalesce\(p_name, ''\)\),\s*'([^']*)',\s*'([^']*)'\)", slug)
        self.assertEqual(len(translate.group(1)), len(translate.group(2)))
        ensure = function_body(SQL, "invite_ensure_code")
        self.assertIn("unique_violation", ensure)
        # Made once and never renamed: shared links keep working.
        self.assertIn("if existing is not null then\n    return existing;", ensure)
        self.assertNotRegex(SQL, r"update public\.invite_codes")

    def test_join_checks_every_rule_in_order_at_any_time(self):
        path, body = newest_definition("invite_join")
        self.assertEqual(path, HARD_PATH)
        at = [body.index(reason) for reason in JOIN_REASONS]
        self.assertEqual(at, sorted(at))
        self.assertNotIn("interval '14 days')", body.replace("gift_end timestamptz := now() + interval '14 days'", ""))
        self.assertIn("email_confirmed_at is null", body)
        self.assertIn("d.user_id = inviter", body)
        self.assertIn("invite_joined_devices", body)
        self.assertIn("public.email_ledger", body)
        self.assertIn("from public.account_emails a", body)
        self.assertIn("if n >= 20 then", body)
        self.assertIn("pg_advisory_xact_lock", body)
        self.assertIn("'invite_gift', 'active', now(), gift_end", body)
        self.assertIn("gift_end timestamptz := now() + interval '14 days'", body)

    def test_a_friend_counts_on_joining(self):
        body = newest_definition("invite_join")[1]
        self.assertIn("'counted', now(),\n          ip, domain_h)", body)
        self.assertLess(body.index("insert into public.invites"), body.index("perform public.invite_make_rewards(inviter);"))
        self.assertIn("default 'counted' check (status in ('counted', 'not_counted'))", table_body(SQL, "invites"))
        me = function_body(SQL, "invite_me")
        self.assertIn("'can_join', not joined", me)
        self.assertIn("jsonb_build_object('reason', coalesce(i.reason, 'removed'))", me)

    def test_email_normalisation(self):
        body = function_body(SQL, "invite_normalize_email")
        self.assertIn("lower(btrim(", body)
        self.assertIn("split_part(left(e, at_pos - 1), '+', 1)", body)
        self.assertIn("domain in ('gmail.com', 'googlemail.com')", body)
        self.assertIn("replace(local_part, '.', '')", body)
        self.assertIn("extensions.hmac(", function_body(SQL, "invite_email_hmac"))

    def test_three_counted_friends_make_a_reward_twelve_a_year(self):
        body = function_body(SQL, "invite_make_rewards")
        self.assertIn("exit when pending < 3;", body)
        self.assertIn("exit when this_year >= 12;", body)
        self.assertIn("interval '365 days'", body)
        self.assertIn("limit 3", body)
        self.assertIn("'invite_reward', 'ready'", body)
        self.assertIn("pg_advisory_xact_lock", body)

    def test_claim(self):
        claim = function_body(SQL, "invite_claim")
        order = ["'not_found'", "'already_claimed'", "'not_eligible'", "'stripe_credit'", "set status = 'active'"]
        at = [claim.index(word) for word in order]
        self.assertEqual(at, sorted(at))
        # Lifetime and admin: refused before anything is written.
        guard = claim[claim.rindex("if exists", 0, at[2]):at[2]]
        self.assertIn("p.is_admin", guard)
        self.assertIn("lp.status = 'granted'", guard)
        self.assertNotIn("update public.pro_grants", claim[:at[3]])
        # A live Stripe subscription (not a manual comp row): SQL changes nothing, the function credits.
        self.assertIn("s.id like 'sub\\_%'", claim)
        self.assertIn("s.status = 'active'", claim)
        # Anyone else: a calendar month stacked after the latest active grant.
        self.assertIn("greatest(now(), max(x.ends_at))", claim)
        self.assertIn("interval '1 month'", claim)

    def test_a_credit_is_marked_only_after_stripe(self):
        grants = table_body(SQL, "pro_grants")
        self.assertIn("check (status in ('ready', 'active', 'used', 'credited'))", grants)
        for column in ("stripe_balance_txn  text", "credit_amount       integer", "credit_currency     text"):
            self.assertIn(column, grants)
        mark = function_body(SQL, "invite_mark_credited")
        self.assertIn("status = 'credited'", mark)
        self.assertIn("and status = 'ready'", mark)
        self.assertIn("g.stripe_balance_txn = p_txn", mark)  # a repeat with the same transaction is fine

    def test_hint_matches_one_code_from_the_last_seven_days(self):
        body = function_body(SQL, "invite_hint")
        self.assertIn("interval '7 days'", body)
        self.assertIn("array_length(codes, 1) <> 1", body)
        self.assertIn("c.user_id <> uid", body)
        self.assertIn("i.invitee_id = uid", body)

    def test_visits_are_kept_fourteen_days_by_a_daily_job(self):
        self.assertIn("delete from public.invite_visits where created_at < now() - interval '14 days';",
                      function_body(SQL, "invite_daily"))
        self.assertIn("if exists (select 1 from pg_extension where extname = 'pg_cron') then", SQL)
        self.assertIn("cron.schedule('invite-daily'", SQL)

    def test_only_first_names_are_shared(self):
        body = function_body(SQL, "invite_first_name")
        self.assertIn("p.full_name", body)
        self.assertIn("split_part(", body)
        me = function_body(SQL, "invite_me")
        self.assertIn("coalesce(n.name, 'A friend')", me)
        self.assertNotIn("email", me)


class TestHardening(unittest.TestCase):
    """202611200001_invites_hardening.sql: the review's findings, on top of the live first migration."""

    NEW = {
        "invite_me": "uuid, text",
        "invite_hint": "uuid, text",
        "invite_join": "uuid, text, text, text, text",
        "invite_claim": "uuid, uuid",
    }
    OLD = {"invite_me": "text", "invite_hint": "text", "invite_join": "text, text, text", "invite_claim": "uuid"}

    def test_A_clients_cannot_call_the_invite_functions(self):
        for name, args in self.OLD.items():
            self.assertIn(f"drop function if exists public.{name}({args});", HARD)
        for name, args in self.NEW.items():
            path, body = newest_definition(name)
            self.assertEqual(path, HARD_PATH, name)
            self.assertIn(f"create or replace function public.{name}(p_user uuid", HARD)
            self.assertIn("uid uuid := p_user;", body, name)
            self.assertNotIn("auth.uid()", body, name)
            self.assertIn(f"revoke all on function public.{name}({args}) from public, anon, authenticated;", HARD)
            self.assertIn(f"grant execute on function public.{name}({args}) to service_role;", HARD)
        self.assertNotRegex(HARD, r"to authenticated")
        # Nothing the edge function reaches reads auth.uid() any more.
        for name in ("invite_ensure_code", "invite_note_device", "invite_make_rewards", "invite_first_name"):
            self.assertNotIn("auth.uid()", newest_definition(name)[1], name)
        for name in ("invite_me", "invite_hint", "invite_join", "invite_claim", "invite_mark_credited",
                     "invite_revert_crediting", "invite_disposable_email", "invite_domain_hmac", "invite_daily",
                     "invite_sync_account_email", "invite_on_user_delete", "grant_lifetime_purchase",
                     "revoke_lifetime_purchase"):
            fn = function_body(HARD, name)
            self.assertIn("security definer", fn, name)
            self.assertIn("set search_path = public, pg_temp", fn, name)

    def test_B_join_order_and_the_farming_limits(self):
        body = newest_definition("invite_join")[1]
        at = [body.index(reason) for reason in JOIN_REASONS]
        self.assertEqual(at, sorted(at))
        # Throwaway domains, on the domain and the domains it belongs to.
        self.assertIn("if public.invite_disposable_email(me.email) then", body)
        # Network: 2 joins in 30 days across all inviters, under the inviter's and the network's locks.
        network = body[body.index("if ip is not null then"):body.index("'same_network'")]
        self.assertIn("pg_advisory_xact_lock(hashtext('invite_network:' || ip))", network)
        self.assertIn("where i.ip_hmac = ip and i.joined_at > now() - interval '30 days';", network)
        self.assertNotIn("inviter_id", network)  # every inviter, so this one too
        self.assertIn("if n >= 2 then", network)
        self.assertLess(body.index("hashtext(inviter::text || ':invite_join')"), body.index("'invite_network:'"))
        # Email domain: 2 a month per inviter unless it is a big public provider.
        domain = body[body.index("if not public.invite_major_domain("):]
        self.assertIn("i.inviter_id = inviter and i.email_domain_hmac = domain_h", domain)
        self.assertIn("interval '30 days'", domain[:domain.index("'limit'")])
        self.assertIn("if n >= 20 then", body)
        self.assertIn("ip, domain_h)", body)
        self.assertIn("add column if not exists ip_hmac text", HARD)
        self.assertIn("add column if not exists email_domain_hmac text", HARD)

    def test_B_major_domains_and_aliases(self):
        major = function_body(HARD, "invite_major_domain")
        for d in ("'gmail.com'", "'outlook.com'", "'hotmail.com'", "'icloud.com'", "'proton.me'", "'mail.ru'", "'aol.com'"):
            self.assertIn(d, major)
        self.assertIn("'^(yahoo|gmx|yandex|hotmail|outlook|live|aol)\\.[a-z]{2,3}(\\.[a-z]{2})?$'", major)
        norm = newest_definition("invite_normalize_email")[1]
        self.assertIn("split_part(left(e, at_pos - 1), '+', 1)", norm)               # +tags everywhere
        self.assertIn("replace(local_part, '.', '')", norm)                           # Gmail dots
        self.assertIn("local_part := split_part(local_part, '-', 1);", norm)          # Yahoo -keyword
        self.assertIn("regexp_replace(local_part, '[._-]', '', 'g')", norm)           # Proton
        self.assertIn("domain ~ '^[a-z0-9-]+\\.fastmail\\.com$'", norm)               # Fastmail subdomains
        self.assertIn("local_part := split_part(domain, '.', 1);", norm)

    def test_B_throwaway_email_domains(self):
        self.assertIn("create table if not exists invite_private.disposable_domains (", HARD)
        self.assertIn("revoke all on invite_private.disposable_domains from anon, authenticated;", HARD)
        block = HARD[HARD.index("insert into invite_private.disposable_domains (domain) values"):HARD.index("on conflict (domain) do nothing;")]
        domains = re.findall(r"\('([^']+)'\)", block)
        self.assertGreaterEqual(len(domains), 50)
        self.assertEqual(len(domains), len(set(domains)))
        for d in domains:
            self.assertRegex(d, r"^[a-z0-9.-]+\.[a-z]{2,}$")
        for d in ("mailinator.com", "guerrillamail.com", "10minutemail.com", "temp-mail.org", "yopmail.com",
                  "trashmail.com", "getnada.com", "sharklasers.com", "dispostable.com", "maildrop.cc",
                  "throwawaymail.com", "mohmal.com", "emailondeck.com", "tempmail.com", "fakeinbox.com"):
            self.assertIn(d, domains)
        for d in ("gmail.com", "outlook.com", "yahoo.com", "icloud.com", "proton.me", "mail.ru"):
            self.assertNotIn(d, domains)
        fn = function_body(HARD, "invite_disposable_email")
        self.assertIn("for i in 1 .. array_length(parts, 1) - 1 loop", fn)  # the domain and its parents, never the TLD alone
        self.assertIn("array_to_string(parts[i:array_length(parts, 1)], '.')", fn)

    def test_C_earlier_accounts_from_an_indexed_table(self):
        self.assertIn("create table if not exists public.account_emails (", HARD)
        self.assertIn("create index if not exists account_emails_hmac_idx on public.account_emails (email_hmac)", HARD)
        self.assertIn("alter table public.account_emails enable row level security;", HARD)
        self.assertIn("revoke all on public.account_emails from anon, authenticated;", HARD)
        self.assertIn("after insert or update of email, email_confirmed_at on auth.users", HARD)
        sync = function_body(HARD, "invite_sync_account_email")
        self.assertIn("exception when others", sync)  # never in the way of a sign-up
        self.assertIn("new.email_confirmed_at is not null", sync)
        backfill = HARD[HARD.index("insert into public.account_emails (user_id, email_hmac, confirmed, created_at)\nselect"):]
        self.assertIn("from auth.users u", backfill)
        body = newest_definition("invite_join")[1]
        self.assertNotIn("from auth.users u\n        where u.id <> uid", body)  # no more scan of every user
        self.assertIn("from public.account_emails a", body)
        self.assertIn("and a.confirmed", body)
        self.assertIn("and a.created_at < me.created_at", body)
        delete = newest_definition("invite_on_user_delete")[1]
        self.assertIn("if old.email_confirmed_at is not null then", delete)
        ledger = HARD[HARD.index("-- The ledger again with the new alias rules"):]
        self.assertIn("u.email_confirmed_at is not null", ledger[:ledger.index("on conflict")])

    def test_D_fail_closed(self):
        body = newest_definition("invite_join")[1]
        self.assertIn("if my_norm is null or email_h is null or domain_h is null then\n"
                      "    return jsonb_build_object('ok', false, 'reason', 'seen_before');", body)

    def test_E_one_credit_per_reward(self):
        self.assertIn("check (status in ('ready', 'active', 'used', 'credited', 'crediting'))", HARD)
        self.assertIn("add column if not exists crediting_at timestamptz", HARD)
        claim = newest_definition("invite_claim")[1]
        crediting = claim.index("set status = 'crediting'")
        self.assertLess(claim.index("pg_advisory_xact_lock"), crediting)
        self.assertLess(crediting, claim.index("'stripe_credit'"))
        self.assertIn("if g.status <> 'ready' then", claim)  # crediting -> already_claimed
        mark = newest_definition("invite_mark_credited")[1]
        self.assertIn("and status = 'crediting'", mark)
        revert = function_body(HARD, "invite_revert_crediting")
        self.assertIn("set status = 'ready', crediting_at = null", revert)
        self.assertIn("and status = 'crediting'", revert)
        daily = newest_definition("invite_daily")[1]
        self.assertIn("status = 'crediting' and crediting_at < now() - interval '1 day'", daily)
        self.assertIn("raise warning", daily)
        self.assertIn("grant execute on function public.invite_revert_crediting(uuid, uuid) to service_role;", HARD)

    def test_join_codes_are_dropped_after_thirty_days(self):
        path, daily = newest_definition("invite_daily")
        self.assertEqual(path, HARD_PATH)
        self.assertIn("update public.invites set ip_hmac = null, email_domain_hmac = null", daily)
        self.assertIn("and joined_at < now() - interval '30 days';", daily)
        self.assertIn("delete from public.invite_visits where created_at < now() - interval '14 days';", daily)
        self.assertIn("perform public.invite_make_rewards(r.inviter_id);", daily)

    def test_F_lifetime_refunds_and_disputes(self):
        self.assertIn("add column if not exists payment_intent_id text", HARD)
        self.assertIn("drop function if exists public.grant_lifetime_purchase(uuid, text);", HARD)
        grant = function_body(HARD, "grant_lifetime_purchase")
        self.assertIn("p_payment_intent text default null", grant)  # the live webhook's 2-argument call still works
        revoke = function_body(HARD, "revoke_lifetime_purchase")
        self.assertIn("set status = 'released'", revoke)
        self.assertIn("where status = 'granted'", revoke)
        for name, args in (("grant_lifetime_purchase", "uuid, text, text"), ("revoke_lifetime_purchase", "text, text")):
            self.assertIn(f"revoke all on function public.{name}({args}) from public, anon, authenticated;", HARD)
            self.assertIn(f"grant execute on function public.{name}({args}) to service_role;", HARD)

    def test_everything_else_in_join_is_unchanged(self):
        old = function_body(SQL, "invite_join")
        new = newest_definition("invite_join")[1]
        for part in ("d.user_id = inviter", "invite_joined_devices", "public.email_ledger", "if n >= 20 then",
                     "'invite_gift', 'active', now(), gift_end", "perform public.invite_make_rewards(inviter);",
                     "on conflict (invitee_id) do nothing;", "email_confirmed_at is null"):
            self.assertIn(part, old, part)
            self.assertIn(part, new, part)


class TestEntitlement(unittest.TestCase):
    def test_is_pro_keeps_every_rule_and_adds_active_grants(self):
        path, body = newest_definition("is_pro")
        self.assertEqual(path, SQL_PATH)
        for rule in ("p.is_admin", "lp.status = 'granted'", "s.status = 'active'",
                     "s.current_period_end is null or s.current_period_end > now()"):
            self.assertIn(rule, body)
        self.assertIn("from public.pro_grants g", body)
        self.assertIn("g.starts_at <= now()", body)
        self.assertIn("g.ends_at > now()", body)
        self.assertNotIn("trialing", body)
        self.assertNotIn("pro_trial_ends_at", body)

    def test_my_entitlement_keeps_its_columns_and_reports_invite(self):
        def columns(body):
            head = body[body.index("returns table (") + len("returns table ("):body.index(")\nlanguage")]
            return [line.strip().rstrip(",") for line in head.strip().splitlines()]

        old = function_body((MIGRATIONS / "202610300001_gusan_pro_pricing.sql").read_text(encoding="utf-8")
                            .replace("drop function if exists public.my_entitlement();\ncreate function",
                                     "create or replace function"), "my_entitlement")
        path, new = newest_definition("my_entitlement")
        self.assertEqual(path, SQL_PATH)
        self.assertEqual(columns(new), columns(old))
        self.assertIn("then 'invite' end", new)
        self.assertIn("stable", new)  # it reads only, as before
        self.assertNotIn("insert", new)
        self.assertNotIn("update", new)
        for part in ("(select plan from sub)", "then 'lifetime' end", "then 'admin' end",
                     "false as trial_available", "(select commitment_ends_at from sub)",
                     "(select cancels_at from sub)"):
            self.assertIn(part, new)
        self.assertLess(new.index("then 'admin' end"), new.index("then 'invite' end"))
        self.assertNotIn("drop function if exists public.my_entitlement", SQL)


class TestEdgeFunction(unittest.TestCase):
    def test_actions_and_auth(self):
        for action in ("me", "hint", "join", "claim", "info", "visit"):
            self.assertIn(f'"{action}"', TS)
        self.assertIn('createClient(SUPABASE_URL, ANON_KEY, {\n    global: { headers: { Authorization: authHeader } }', TS)
        self.assertIn("supa.auth.getUser()", TS)
        self.assertIn('json({ error: "unauthorized" }, 401)', TS)
        self.assertIn("Deploy with JWT verification DISABLED", TS)
        # Every SQL call runs with the service role and the user id auth.getUser() returned.
        self.assertNotIn("supa.rpc(", TS)
        self.assertIn("const userId = userData.user.id;", TS)
        for fn in ("invite_me", "invite_hint", "invite_join", "invite_claim"):
            self.assertRegex(TS, rf'db\.rpc\("{fn}", \{{\s*p_user: userId,')
        for rpc in ("invite_info", "invite_record_visit", "invite_mark_credited", "invite_revert_crediting"):
            self.assertIn(f'"{rpc}"', TS)
        join = TS[TS.index('if (action === "join")'):TS.index('if (action === "claim")')]
        self.assertIn('p_ip_hmac: network ? await hmacHex("ip", network) : null,', join)
        self.assertIn("const network = normalizeIp(clientIp(req));", join)

    def test_contract_fields(self):
        self.assertIn("link: `${SITE}/i/${me.code}`", TS)
        self.assertIn('const SITE = "https://gusan.aibuben.xyz";', TS)
        self.assertIn("{ found: true, name: hint.name, code: hint.code }", TS)
        self.assertIn("{ ok: true, name: result.name ?? null, gift_ends_at: result.gift_ends_at }", TS)
        self.assertIn('{ ok: true, status: "next_bill_free", amount: charge.amount, currency: charge.currency }', TS)
        self.assertIn('{ ok: false, reason: "stripe_error" }', TS)
        me = newest_definition("invite_me")[1]
        for name in ("code", "first_name", "counted", "joined_total", "rewards_left_this_year", "friends",
                     "rewards", "gift", "invited_by", "can_join"):
            self.assertIn(f"'{name}'", me, name)
        join = newest_definition("invite_join")[1]
        for reason in JOIN_REASONS:
            self.assertIn(reason, join, reason)
            self.assertIn(reason.strip("'"), TS, reason)
        for reason in ("not_found", "already_claimed", "not_eligible", "stripe_error"):
            self.assertIn(reason, TS, reason)

    def test_the_stripe_credit(self):
        self.assertIn('Deno.env.get("STRIPE_SECRET_KEY")', TS)
        self.assertIn("`/subscriptions/${encodeURIComponent(subscriptionId)}?expand[]=items.data.price`", TS)
        self.assertIn("`/customers/${encodeURIComponent(charge.customer)}/balance_transactions`", TS)
        self.assertIn("amount: String(-charge.amount)", TS)
        self.assertIn('const CREDIT_DESCRIPTION = "Gusan invite: 1 free month";', TS)
        self.assertIn('"metadata[grant_id]": grantId', TS)
        self.assertIn('headers["Idempotency-Key"] = idempotencyKey', TS)
        self.assertIn('const STRIPE_VERSION = "2024-06-20";', TS)
        # One month: yearly / 12, every n months / n, day or week refused.
        self.assertIn('const months = interval === "month" ? count : interval === "year" ? 12 * count : 0;', TS)
        self.assertIn("if (!months) return null;", TS)
        self.assertIn("total += (unit * quantity) / months;", TS)
        self.assertIn("const amount = Math.round(total);", TS)
        credit = TS[TS.index("async function creditNextBill"):TS.index("// --- the website worker")]
        # An earlier credit for this reward is looked for first and used again.
        self.assertLess(credit.index("findCredit("), credit.index('"metadata[grant_id]"'))
        self.assertIn("?limit=100", credit)
        # Stripe first, then the database; a Stripe failure puts the reward back to ready.
        self.assertLess(credit.index("balance_transactions"), credit.index('"invite_mark_credited"'))
        self.assertIn('db.rpc("invite_revert_crediting", { p_user: userId, p_grant_id: grantId })', credit)

    def test_the_salt_is_required_and_the_worker_key(self):
        self.assertIn('Deno.env.get("INVITE_SALT")', TS)
        self.assertNotIn("gusan-invite-salt", TS)  # no fallback key
        entry = TS[TS.index("Deno.serve("):]
        self.assertLess(entry.index("if (!INVITE_SALT)"), entry.index("JSON.parse"))
        self.assertIn('json({ error: "not_configured" }, 503)', entry)
        self.assertIn('Deno.env.get("INVITE_WORKER_KEY")', TS)
        self.assertIn('req.headers.get("x-gusan-worker")', TS)
        self.assertIn('{ name: "HMAC", hash: "SHA-256" }', TS)
        self.assertIn('req.headers.get("cf-connecting-ip")', TS)
        self.assertIn('req.headers.get("x-forwarded-for")', TS)

    def test_nothing_personal_is_logged(self):
        # One console call, behind log(action, outcome); outcomes are fixed words or reasons.
        self.assertEqual(re.findall(r"console\.(?:log|error|warn)\(([^)]*)\)", TS), ['"invite", action, outcome'])
        calls = re.findall(r"\blog\(([^;]*)\);", TS)
        self.assertGreater(len(calls), 15)
        for call in calls:
            for leak in ("body.", "code)", "ip)", "ipHmac", "device)", "deviceHmac", "name", "email", "user",
                         "id)", "txn", "customer", "subscription", "amount"):
                self.assertNotIn(leak, call)

    def test_limits_are_per_network(self):
        self.assertIn('export const networkKey = (raw: string) => normalizeIp(raw) ?? "unknown";', TS)
        self.assertIn("limited(visitor ? `info:v:${networkKey(visitor)}` : `info:${networkKey(clientIp(req))}`, 30)", TS)
        self.assertIn("limited(`hint:${network ?? \"unknown\"}`, 30)", TS)
        self.assertIn("limited(`user:${userId}`, 30)", TS)
        self.assertIn('"Retry-After"', TS)
        self.assertIn("const MAX_BODY_BYTES = 4096;", TS)


if __name__ == "__main__":
    unittest.main()
