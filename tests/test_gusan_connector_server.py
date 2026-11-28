"""The Gusan connector, server side (docs/mcp/gusan-connector-spec.md): the migration, the token guard in the
functions that act for the signed-in person, the `confirmed` redirect and the MCP function.

There is no database or Deno here, so like test_invite_server.py these read the sources and pin the rules:
an AI assistant's token (it carries client_id) can read the person's meetings, plan and usage, and nothing
else; the app's own sign-in writes the cloud copy. The MCP logic itself is tested with Node
(supabase/functions/mcp/server.test.ts), which this runs when Node is there."""
import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FUNCTIONS = ROOT / "supabase" / "functions"
SQL = (ROOT / "supabase" / "migrations" / "202611280001_cloud_meetings.sql").read_text(encoding="utf-8")
MCP = (FUNCTIONS / "mcp" / "index.ts").read_text(encoding="utf-8")
SERVER = (FUNCTIONS / "mcp" / "server.ts").read_text(encoding="utf-8")
CONFIRMED = (FUNCTIONS / "confirmed" / "index.ts").read_text(encoding="utf-8")


def policy(name):
    m = re.search(r'create policy "%s" on public\.\w+\s*(.*?);' % re.escape(name), SQL, re.S)
    assert m, name
    return m.group(1)


class Migration(unittest.TestCase):
    def test_tables_have_row_level_security_and_no_anonymous_access(self):
        for table in ("cloud_meetings", "app_usage_stats"):
            self.assertIn(f"alter table public.{table} enable row level security;", SQL)
            self.assertIn(f"revoke all on table public.{table} from anon, public;", SQL)
            self.assertRegex(SQL, rf"references auth\.users \(id\) on delete cascade[\s\S]*{table}|create table if not exists public\.{table}")

    def test_only_the_app_writes_the_cloud_copy_and_assistants_only_read(self):
        self.assertIn("user_id = (select auth.uid())", policy("read own cloud meetings"))
        self.assertNotIn("client_id", policy("read own cloud meetings"))
        for name in ("app adds own cloud meetings", "app updates own cloud meetings", "app removes own cloud meetings",
                     "app manages own usage stats"):
            body = policy(name)
            self.assertIn("user_id = (select auth.uid())", body, name)
            self.assertIn("(select auth.jwt() ->> 'client_id') is null", body, name)

    def test_reader_functions_run_as_the_caller(self):
        for fn in ("cloud_meetings_search", "cloud_meetings_list", "cloud_meetings_get", "cloud_meetings_transcript"):
            start = SQL.index(f"create or replace function public.{fn}(")
            body = SQL[start:SQL.index("$$;", SQL.index("as $$", start)) + 3]
            self.assertIn("security invoker", body, fn)
            self.assertIn("(select auth.uid())", body, fn)
            self.assertIn(f"revoke all on function public.{fn}(", SQL)
            self.assertRegex(SQL, rf"grant execute on function public\.{fn}\([^)]*\) to authenticated;")

    def test_assistant_tokens_reach_only_the_readers_and_the_plan_over_rest(self):
        start = SQL.index("create or replace function public.gusan_api_pre_request()")
        body = SQL[start:SQL.index("$$;", start) + 3]
        self.assertIn("(claims::jsonb ->> 'client_id') is null", body)
        allowed = re.search(r"~ '(.*?)'", body).group(1)
        self.assertEqual(allowed, "/rpc/(cloud_meetings_(search|list|get|transcript)|my_entitlement)$")
        self.assertIn("raise insufficient_privilege", body)
        self.assertIn("alter role authenticator set pgrst.db_pre_request = 'public.gusan_api_pre_request';", SQL)
        self.assertIn("notify pgrst, 'reload config';", SQL)

    def test_search_words_cannot_break_the_query(self):
        start = SQL.index("create or replace function public.cloud_meetings_tsquery(")
        body = SQL[start:SQL.index("$$;", start)]
        self.assertIn("quote_literal(w) || ':*'", body)
        self.assertIn("limit 12", body)


class FunctionGuard(unittest.TestCase):
    def test_every_function_that_acts_for_the_person_refuses_an_assistant_token_first(self):
        guarded = []
        for index in sorted(FUNCTIONS.glob("*/index.ts")):
            name = index.parent.name
            src = index.read_text(encoding="utf-8")
            if name == "mcp" or "getUser" not in src or "isAssistantToken" not in src:
                continue
            guarded.append(name)
            self.assertIn('import { assistantRefusal, isAssistantToken } from "../_shared/assistant_token.ts";', src, name)
            auth = src.index('const authHeader = req.headers.get("Authorization") ?? "";')
            guard = src.index("if (isAssistantToken(authHeader)) return assistantRefusal(", auth)
            self.assertLess(guard - auth, 200, name)
            self.assertLess(guard, src.index("getUser", auth), name)
        # The four deployed with the check (2026-11-28, added to their live copies). The others act only
        # after a Pro or quota check made with the caller's own token, which gusan_api_pre_request refuses
        # for an assistant (see the next test); create-checkout at most makes a payment link.
        self.assertEqual(guarded, ["billing-portal", "delete-account", "invite", "submit-feedback"])

    def test_the_others_check_pro_with_the_callers_token_before_any_work(self):
        for name in ("live-stt", "smart-action", "transcribe-proxy"):
            src = (FUNCTIONS / name / "index.ts").read_text(encoding="utf-8")
            self.assertIn('supa.rpc("is_pro")', src, name)
            self.assertIn("const supa = createClient(SUPABASE_URL, ANON_KEY", src, name)

    def test_the_guard_reads_client_id_and_only_refuses(self):
        src = (FUNCTIONS / "_shared" / "assistant_token.ts").read_text(encoding="utf-8")
        self.assertIn('typeof claims?.client_id === "string" && claims.client_id.length > 0', src)
        self.assertIn("status: 403", src)


class Confirmed(unittest.TestCase):
    def test_the_consent_path_goes_to_the_site_page_and_everything_else_to_confirmed(self):
        self.assertIn('const CONSENT = "https://gusan.aibuben.xyz/oauth/consent";', CONFIRMED)
        self.assertIn('const PAGE = "https://gusan.aibuben.xyz/confirmed";', CONFIRMED)
        self.assertIn("/\\/oauth\\/consent\\/?$/.test(url.pathname) ? CONSENT : PAGE", CONFIRMED)
        self.assertIn("return page + url.search;", CONFIRMED)


class Mcp(unittest.TestCase):
    def test_sign_in_is_checked_and_points_to_the_resource_metadata(self):
        self.assertIn('const RESOURCE_METADATA = "https://gusan.aibuben.xyz/.well-known/oauth-protected-resource/mcp";', MCP)
        self.assertIn("WWW-Authenticate", MCP)
        self.assertIn("/auth/v1/user", MCP)

    def test_meetings_are_read_with_the_persons_token_and_nothing_is_written(self):
        for fn in ("cloud_meetings_search", "cloud_meetings_list", "cloud_meetings_get", "cloud_meetings_transcript",
                   "my_entitlement"):
            self.assertIn(f'rpc<', MCP)
            self.assertIn(f'"{fn}"', MCP)
        self.assertNotRegex(MCP, r'method: "(PATCH|PUT|DELETE)"')
        self.assertEqual(MCP.count('method: "POST"'), 1)  # the rpc() reader (PostgREST runs functions with POST)
        for path in re.findall(r"service<[^>]*>\(`([a-z_]+)\?", MCP):
            self.assertIn(path, {"cloud_meetings", "app_usage_stats", "profiles", "pro_grants", "invites",
                                 "lifetime_purchases", "cloud_usage"})
        self.assertRegex(MCP, r"user_id=eq\.\$\{uid\}")

    def test_every_tool_is_read_only(self):
        self.assertIn("readOnlyHint: true, destructiveHint: false, idempotentHint: true, openWorldHint: false", SERVER)
        self.assertEqual(SERVER.count("annotations: { title:"), 6)

    @unittest.skipUnless(shutil.which("node"), "Node is not installed")
    def test_node_suite(self):
        out = subprocess.run(["node", "--test", str(FUNCTIONS / "mcp" / "server.test.ts")], capture_output=True,
                             text=True, timeout=120)
        self.assertEqual(out.returncode, 0, out.stdout[-2000:] + out.stderr[-2000:])


if __name__ == "__main__":
    unittest.main()
