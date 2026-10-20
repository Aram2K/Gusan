"""Notion connect: state and authorize URL, the loopback listener, the edge
function calls, and the Notion page helpers - all against fakes.

Pure stdlib + mock - no network, no Qt. The listener tests talk to a real
socket on 127.0.0.1 (the way the browser would); everything else is a fake
``requests``.
"""
import contextlib
import http.client
import logging
import re
import socket
import threading
import time
import unittest
import urllib.parse
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import notion_connect as nc
import notion_export as ne

_real_sleep = time.sleep      # FakeHttp patches time.sleep; a few tests need a real pause

TOKEN = "ntn_test_token_123"
HUB_ID = "0123456789abcdef0123456789abcdef"
HUB_DASHED = "01234567-89ab-cdef-0123-456789abcdef"
SECRET_WORDS = ("SECRET-CODE-XYZ", "SECRET-ACCESS-XYZ", "SECRET-REFRESH-XYZ")


class Offline(Exception):
    """Stands in for requests' errors: tests/test_core replaces requests with a
    stub that has no exception classes, so a test patches RequestException
    with this one instead of raising requests.ConnectionError."""


def _resp(status=200, body=None, headers=None):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = {} if body is None else body
    r.headers = headers or {}
    return r


def _tokens(**over):
    d = {"access_token": "at1", "refresh_token": "rt1", "workspace_id": "ws1",
         "workspace_name": "Aram's workspace", "workspace_icon": None,
         "bot_id": "bot1", "duplicated_template_id": None}
    d.update(over)
    return d


@contextlib.contextmanager
def _captured_logs():
    """Everything the notion loggers write, rendered (traceback included)."""
    lines = []

    class Grab(logging.Handler):
        def emit(self, record):
            lines.append(self.format(record))

    handler = Grab(level=logging.DEBUG)
    log = logging.getLogger("transcribe.notion")
    old_level = log.level
    log.addHandler(handler)
    log.setLevel(logging.DEBUG)
    try:
        yield lines
    finally:
        log.removeHandler(handler)
        log.setLevel(old_level)


class FakeHttp:
    """requests.get/post/patch in one place. ``add(method, path, *answers)``:
    answers are used in order and the last one repeats; an answer is a
    response, an exception (raised) or a callable(call) -> response. ``path``
    is relative to the Notion API, or the full function URL."""

    def __init__(self):
        self.routes = {}
        self.calls = []

    def add(self, method, path, *answers):
        self.routes[(method.upper(), path)] = list(answers)
        return self

    def _sender(self, method):
        def send(url, **kw):
            path = url[len(nc.API_BASE):] if url.startswith(nc.API_BASE) else url
            call = SimpleNamespace(method=method, path=path, url=url,
                                   headers=kw.get("headers") or {}, json=kw.get("json"),
                                   timeout=kw.get("timeout"))
            self.calls.append(call)
            queue = self.routes.get((method, path))
            if not queue:
                raise AssertionError(f"unexpected {method} {path}")
            item = queue.pop(0) if len(queue) > 1 else queue[0]
            if isinstance(item, Exception):
                raise item
            return item(call) if callable(item) and not isinstance(item, MagicMock) else item
        return send

    def patched(self):
        stack = contextlib.ExitStack()
        # notion_export and notion_connect can hold different `requests` objects
        # (tests/test_core swaps in a stub between their imports): patch each.
        for mod in {id(m): m for m in (ne.requests, nc.requests)}.values():
            for method in ("get", "post", "patch"):
                stack.enter_context(patch.object(mod, method, side_effect=self._sender(method.upper())))
            stack.enter_context(patch.object(mod, "RequestException", Offline))
        stack.enter_context(patch.object(ne.time, "sleep"))
        return stack

    def on(self, method, path):
        return [c for c in self.calls if c.method == method.upper() and c.path == path]


class NetCase(unittest.TestCase):
    """A test with a fake network and a clean refresh history."""

    def setUp(self):
        self.http = FakeHttp()
        stack = self.http.patched()
        stack.__enter__()
        self.addCleanup(stack.__exit__, None, None, None)
        nc.forget_tokens()
        self.addCleanup(nc.forget_tokens)


def _page(n, title="Page", prop="title", **extra):
    page = {"object": "page", "id": f"{n:08x}-1111-2222-3333-{n:012x}",
            "url": f"https://www.notion.so/Page-{n}",
            "parent": {"type": "workspace", "workspace": True},
            "properties": {prop: {"id": "title", "type": "title",
                                  "title": ([{"type": "text", "plain_text": title,
                                              "text": {"content": title}}] if title else [])}}}
    page.update(extra)
    return page


def _search(results, more=False, cursor=None):
    return _resp(200, {"object": "list", "results": results, "has_more": more,
                       "next_cursor": cursor})


# ── state and authorize URL ──────────────────────────────────────────────────
class TestState(unittest.TestCase):
    def test_round_trip(self):
        state = nc.build_state("abcdefghijklmnop", 54321)
        self.assertEqual(state, "abcdefghijklmnop.54321")
        self.assertEqual(nc.parse_state(state), ("abcdefghijklmnop", 54321))

    def test_real_nonces_are_accepted(self):
        import secrets
        for _ in range(50):
            self.assertIsNotNone(nc.parse_state(nc.build_state(secrets.token_urlsafe(18), 49152)))

    def test_what_the_function_would_refuse(self):
        good = "abcdefghijklmnop"
        for bad in (
            f"{good[:15]}.54321",               # nonce too short
            f"{'a' * 65}.54321",                # nonce too long
            f"{good}.1023", f"{good}.65536",    # port out of range
            f"{good}.123", f"{good}.123456",    # port wrong length
            f"{good}.5432a", f"{good}.",        # port not digits / missing
            f"{good}54321", f"{good}..54321",   # no / double dot
            f"{good}.54321\n",                  # a trailing newline must not slip through
            f"{good}!.54321", f"{good[:-1]}é.54321",
            "", None, 54321, b"abcdefghijklmnop.54321",
        ):
            self.assertIsNone(nc.parse_state(bad), repr(bad))

    def test_port_edges(self):
        good = "abcdefghijklmnop"
        self.assertEqual(nc.parse_state(f"{good}.1024")[1], 1024)
        self.assertEqual(nc.parse_state(f"{good}.65535")[1], 65535)


class TestAuthorizeUrl(unittest.TestCase):
    def test_the_documented_parameters(self):
        url = nc.authorize_url("client-123", "abcdefghijklmnop.54321")
        parts = urllib.parse.urlsplit(url)
        self.assertEqual(f"{parts.scheme}://{parts.netloc}{parts.path}", nc.AUTH_URL)
        q = urllib.parse.parse_qs(parts.query)
        self.assertEqual({k: v[0] for k, v in q.items()}, {
            "client_id": "client-123", "response_type": "code", "owner": "user",
            "redirect_uri": nc.REDIRECT_URI, "state": "abcdefghijklmnop.54321"})

    def test_the_redirect_is_the_one_registered_https_address(self):
        self.assertEqual(nc.REDIRECT_URI, nc.FUNCTION_URL)
        self.assertTrue(nc.REDIRECT_URI.startswith("https://hftcelxzfoubheqeoool.supabase.co/"))
        self.assertTrue(nc.REDIRECT_URI.endswith("/functions/v1/notion-oauth"))

    def test_configured_follows_the_client_id(self):
        with patch.object(nc, "NOTION_CLIENT_ID", ""):
            self.assertFalse(nc.configured())
        with patch.object(nc, "NOTION_CLIENT_ID", "  "):
            self.assertFalse(nc.configured())
        with patch.object(nc, "NOTION_CLIENT_ID", "abc"):
            self.assertTrue(nc.configured())

    def test_ships_unconfigured_until_the_founder_gives_the_id(self):
        # Flip this assertion the day the real (public) client id is pasted in.
        self.assertEqual(nc.NOTION_CLIENT_ID, "")


class TestMessages(unittest.TestCase):
    def test_the_sentences_of_the_spec(self):
        self.assertEqual(nc.MESSAGES["denied"], "You cancelled in Notion. Nothing was connected.")
        self.assertEqual(nc.MESSAGES["timeout"], "That took too long. Nothing was connected.")
        self.assertEqual(nc.MESSAGES["offline"], "Can't reach Notion. Check your internet and try again.")
        self.assertEqual(nc.MESSAGES["port_busy"],
                         "Couldn't start the connection on this computer. Close other Gusan windows and try again.")
        self.assertEqual(nc.MESSAGES["expired"], "That Notion sign-in expired. Try again.")
        self.assertEqual(nc.MESSAGES["server"], "Notion couldn't finish connecting. Try again in a minute.")
        self.assertEqual(nc.MESSAGES["browser"], "Couldn't open your browser. Try again.")
        self.assertEqual(nc.MESSAGES["rejected"],
                         "Notion didn't accept that secret. Copy it again from your integration.")
        self.assertEqual(nc.MESSAGES["reconnect"], "Notion needs to be connected again.")
        self.assertEqual(nc.MESSAGES["other"], "Couldn't connect to Notion. Try again.")

    def test_an_error_reads_as_its_sentence(self):
        for code, sentence in nc.MESSAGES.items():
            e = nc.ConnectError(code)
            self.assertEqual(e.code, code)
            self.assertEqual(str(e), sentence)

    def test_an_unknown_code_is_other(self):
        e = nc.ConnectError("whatever")
        self.assertEqual((e.code, str(e)), ("other", nc.MESSAGES["other"]))

    def test_cancelled_is_a_connect_error(self):
        e = nc.ConnectCancelled()
        self.assertIsInstance(e, nc.ConnectError)
        self.assertEqual(e.code, "cancelled")

    def test_no_message_names_a_technical_word_or_the_old_name(self):
        for sentence in nc.MESSAGES.values():
            for word in ("OAuth", "token", "HTTP", "Transcribe", "workspace id"):
                self.assertNotIn(word, sentence)


# ── the browser landing page ─────────────────────────────────────────────────
class TestLandingHtml(unittest.TestCase):
    def test_every_kind_says_the_true_thing(self):
        ok = nc.landing_html("ok")
        self.assertIn("Almost done.", ok)
        self.assertIn("Go back to Gusan to finish. You can close this tab.", ok)
        err = nc.landing_html("error")
        self.assertIn("Notion wasn't connected.", err)
        self.assertIn("Go back to Gusan and try again.", err)
        denied = nc.landing_html("denied")
        self.assertIn("Cancelled.", denied)
        self.assertIn("Nothing was connected.", denied)

    def test_an_unknown_kind_is_the_error_page(self):
        self.assertEqual(nc.landing_html("nope"), nc.landing_html("error"))

    def test_light_and_dark_and_no_outside_request(self):
        for kind in ("ok", "error", "denied"):
            page = nc.landing_html(kind)
            self.assertIn("prefers-color-scheme", page)
            self.assertIn('name="color-scheme" content="light dark"', page)
            self.assertNotIn("http", page)             # no resource, link or xmlns to fetch
            self.assertNotIn("<script", page)
            self.assertNotIn("<link", page)
            self.assertNotIn("Transcribe", page)
            self.assertTrue(page.startswith("<!doctype html>"))

    def test_explicit_colours_for_both_schemes(self):
        page = nc.landing_html("ok")
        for colour in ("#eaf0f8", "#0f172a", "#1e293b", "#334155", "#cbd5e1", "#16a34a",
                       "#dcfce7", "#4ade80", "#b91c1c", "#fee2e2", "#f87171"):
            self.assertIn(colour, page)
        self.assertIn("background-color:#eaf0f8", page)       # never the browser default

    def test_the_tile_is_a_check_or_a_cross(self):
        self.assertIn('class="tile check"', nc.landing_html("ok"))
        self.assertIn('class="tile cross"', nc.landing_html("error"))
        self.assertIn('class="tile cross"', nc.landing_html("denied"))


# ── the loopback listener ────────────────────────────────────────────────────
class TestConnect(unittest.TestCase):
    TOK = _tokens(access_token="at-new", refresh_token="rt-new")

    def setUp(self):
        for patcher in (patch.object(nc, "NOTION_CLIENT_ID", "client-123"),
                        patch.object(nc.requests, "RequestException", Offline)):
            patcher.start()
            self.addCleanup(patcher.stop)
        nc.forget_tokens()
        self.addCleanup(nc.forget_tokens)

    def _run(self, hits, answer=None, timeout=5, cancel=None, delay=0.0):
        """Run connect() and replay ``hits`` (paths; "{state}" becomes the real
        state) against its listener, one after the other. Returns what
        happened."""
        out = {"statuses": [], "bodies": [], "urls": [], "code_calls": 0, "error": None, "tok": None}

        def fake_browser(url):
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            state = q["state"][0]
            out["port"] = nc.parse_state(state)[1]

            def hit_all():
                time.sleep(delay)
                for h in hits:
                    conn = http.client.HTTPConnection("127.0.0.1", out["port"], timeout=5)
                    conn.request("GET", h.replace("{state}", state))
                    resp = conn.getresponse()
                    out["statuses"].append(resp.status)
                    out["bodies"].append(resp.read().decode("utf-8"))
                    out["headers"] = dict(resp.getheaders())
                    conn.close()
            t = threading.Thread(target=hit_all, daemon=True)
            out["thread"] = t
            t.start()
            return True

        def on_url(url):
            out["urls"].append(url)

        def on_code():
            out["code_calls"] += 1

        answer = answer if answer is not None else _resp(200, self.TOK)
        if isinstance(answer, MagicMock):
            kwargs = {"return_value": answer}
        else:                                   # an exception to raise, or a function
            kwargs = {"side_effect": answer}
        with patch.object(nc.requests, "post", **kwargs) as post:
            try:
                out["tok"] = nc.connect(open_browser=fake_browser, timeout=timeout, cancel=cancel,
                                        on_url=on_url, on_code=on_code)
            except nc.ConnectError as e:
                out["error"] = e
            out["post"] = post
        if "thread" in out:
            out["thread"].join(5)
        return out

    def test_the_code_is_swapped_for_tokens(self):
        out = self._run(["/notion?code=SECRET-CODE-XYZ&state={state}"])
        self.assertIsNone(out["error"], out["error"])
        self.assertEqual(out["tok"]["access_token"], "at-new")
        self.assertEqual(out["tok"]["refresh_token"], "rt-new")
        self.assertEqual(out["tok"]["workspace_name"], "Aram's workspace")
        out["post"].assert_called_once()
        self.assertEqual(out["post"].call_args.args[0], nc.FUNCTION_URL)
        self.assertEqual(out["post"].call_args.kwargs["json"], {
            "action": "exchange", "code": "SECRET-CODE-XYZ", "redirect_uri": nc.REDIRECT_URI})
        self.assertEqual(out["post"].call_args.kwargs["timeout"], nc.HTTP_TIMEOUT)

    def test_the_tab_gets_the_landing_page_and_safe_headers(self):
        out = self._run(["/notion?code=abc&state={state}"])
        self.assertEqual(out["statuses"], [200])
        self.assertEqual(out["bodies"][0], nc.landing_html("ok"))
        h = {k.lower(): v for k, v in out["headers"].items()}
        self.assertEqual(h["content-type"], "text/html; charset=utf-8")
        self.assertEqual(h["cache-control"], "no-store")
        self.assertEqual(h["referrer-policy"], "no-referrer")
        self.assertIn("default-src 'none'", h["content-security-policy"])
        self.assertEqual(int(h["content-length"]), len(out["bodies"][0].encode("utf-8")))

    def test_on_url_is_called_once_with_the_authorize_url(self):
        out = self._run(["/notion?code=abc&state={state}"])
        self.assertEqual(len(out["urls"]), 1)
        url = out["urls"][0]
        self.assertTrue(url.startswith(nc.AUTH_URL + "?"))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["client_id"], ["client-123"])
        nonce, port = nc.parse_state(q["state"][0])
        self.assertEqual(port, out["port"])
        self.assertEqual(out["code_calls"], 1)

    def test_every_attempt_has_its_own_state_and_port_is_the_listeners(self):
        a = self._run(["/notion?code=abc&state={state}"])
        b = self._run(["/notion?code=abc&state={state}"])
        self.assertNotEqual(a["urls"][0], b["urls"][0])

    def test_a_forged_or_stray_request_does_not_end_the_wait(self):
        out = self._run([
            "/notion?error=access_denied",                   # no state at all
            "/notion?code=evil&state=abcdefghijklmnop.54321",  # a state that isn't ours
            "/notion?code=evil&state=",                      # an empty one
            "/favicon.ico",                                  # the browser's own request
            "/notion",                                       # nothing in it
            "/other?code=evil&state={state}",                # right state, wrong path
            "/notion?code=real&state={state}",
        ])
        self.assertIsNone(out["error"], out["error"])
        self.assertEqual(out["statuses"], [400, 400, 400, 404, 404, 404, 200])
        self.assertEqual(out["post"].call_args.kwargs["json"]["code"], "real")

    def test_an_oversized_code_is_refused_and_the_wait_goes_on(self):
        out = self._run(["/notion?code=" + "x" * 2001 + "&state={state}",
                         "/notion?code=&state={state}",
                         "/notion?code=real&state={state}"])
        self.assertEqual(out["statuses"], [400, 404, 200])      # a blank code is no code at all
        self.assertEqual(out["tok"]["access_token"], "at-new")

    def test_too_many_query_fields_are_refused(self):
        junk = "&".join(f"a{i}=1" for i in range(40))
        out = self._run([f"/notion?{junk}", "/notion?code=real&state={state}"])
        self.assertEqual(out["statuses"], [404, 200])

    def test_the_user_said_no(self):
        out = self._run(["/notion?error=access_denied&state={state}"])
        self.assertEqual(out["error"].code, "denied")
        self.assertEqual(str(out["error"]), nc.MESSAGES["denied"])
        self.assertIn("Cancelled.", out["bodies"][0])
        out["post"].assert_not_called()
        self.assertEqual(out["code_calls"], 0)

    def test_any_other_error_from_notion(self):
        out = self._run(["/notion?error=server_error&state={state}"])
        self.assertEqual(out["error"].code, "other")
        self.assertIn("Notion wasn't connected.", out["bodies"][0])
        out["post"].assert_not_called()

    def test_nothing_before_the_deadline_is_a_timeout(self):
        start = time.monotonic()
        out = self._run([], timeout=1)
        self.assertEqual(out["error"].code, "timeout")
        self.assertEqual(str(out["error"]), nc.MESSAGES["timeout"])
        self.assertLess(time.monotonic() - start, 5)
        out["post"].assert_not_called()

    def test_the_listener_is_closed_afterwards(self):
        for hits, timeout in ((["/notion?code=abc&state={state}"], 5), ([], 1)):
            out = self._run(hits, timeout=timeout)
            with self.assertRaises(OSError):
                socket.create_connection(("127.0.0.1", out["port"]), timeout=0.5).close()

    def test_an_idle_connection_cannot_pin_the_wait(self):
        socks = []

        def browser(url):
            port = nc.parse_state(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0])[1]
            socks.append(socket.create_connection(("127.0.0.1", port)))   # connects, never sends
            state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

            def hit():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", f"/notion?code=abc&state={state}")
                conn.getresponse().read()
                conn.close()
            threading.Thread(target=hit, daemon=True).start()
            return True

        self.addCleanup(lambda: [s.close() for s in socks])
        start = time.monotonic()
        with patch.object(nc.requests, "post", return_value=_resp(200, self.TOK)):
            tok = nc.connect(open_browser=browser, timeout=10)
        self.assertEqual(tok["access_token"], "at-new")
        self.assertLess(time.monotonic() - start, 3)       # the idle socket would hold it for 5 s

    def test_an_idle_connection_alone_ends_in_a_timeout(self):
        socks = []

        def browser(url):
            port = nc.parse_state(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0])[1]
            socks.append(socket.create_connection(("127.0.0.1", port)))
            return True

        self.addCleanup(lambda: [s.close() for s in socks])
        start = time.monotonic()
        with self.assertRaises(nc.ConnectError) as ctx:
            nc.connect(open_browser=browser, timeout=1)
        self.assertEqual(ctx.exception.code, "timeout")
        self.assertLess(time.monotonic() - start, 4)

    def test_no_browser_fails_at_once_and_closes_the_listener(self):
        ports = []
        start = time.monotonic()
        with self.assertRaises(nc.ConnectError) as ctx:
            nc.connect(open_browser=lambda url: False, timeout=30,
                       on_url=lambda url: ports.append(
                           nc.parse_state(urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0])[1]))
        self.assertEqual(ctx.exception.code, "browser")
        self.assertEqual(str(ctx.exception), nc.MESSAGES["browser"])
        self.assertLess(time.monotonic() - start, 2)
        with self.assertRaises(OSError):
            socket.create_connection(("127.0.0.1", ports[0]), timeout=0.5).close()

    def test_a_browser_that_raises_is_no_browser(self):
        def boom(url):
            raise RuntimeError("no display")
        with self.assertRaises(nc.ConnectError) as ctx:
            nc.connect(open_browser=boom, timeout=30)
        self.assertEqual(ctx.exception.code, "browser")

    def test_a_listener_that_cannot_start_is_port_busy(self):
        browser = MagicMock()
        with patch.object(nc, "_LoopbackServer", side_effect=OSError("in use")):
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.connect(open_browser=browser)
        self.assertEqual(ctx.exception.code, "port_busy")
        self.assertEqual(str(ctx.exception), nc.MESSAGES["port_busy"])
        browser.assert_not_called()

    def test_without_a_client_id_nothing_opens(self):
        browser = MagicMock()
        with patch.object(nc, "NOTION_CLIENT_ID", ""):
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.connect(open_browser=browser)
        self.assertEqual(ctx.exception.code, "other")
        browser.assert_not_called()

    def test_a_cancel_set_before_the_start_opens_no_browser(self):
        cancel = threading.Event()
        cancel.set()
        browser = MagicMock()
        with self.assertRaises(nc.ConnectCancelled):
            nc.connect(open_browser=browser, cancel=cancel)
        browser.assert_not_called()

    def test_cancel_stops_the_waiting_listener_quickly(self):
        cancel = threading.Event()

        def browser(url):
            threading.Timer(0.3, cancel.set).start()
            return True

        start = time.monotonic()
        with self.assertRaises(nc.ConnectCancelled):
            nc.connect(open_browser=browser, timeout=30, cancel=cancel)
        self.assertLess(time.monotonic() - start, 3)

    def test_an_answer_that_arrives_with_the_cancel_is_not_used(self):
        cancel = threading.Event()

        def browser(url):
            cancel.set()                                     # Cancel and the redirect at the same moment
            state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
            port = nc.parse_state(state)[1]

            def hit():
                try:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", f"/notion?code=abc&state={state}")
                    conn.getresponse().read()
                except OSError:
                    pass                                     # the listener may already be closed
            threading.Thread(target=hit, daemon=True).start()
            return True

        with patch.object(nc.requests, "post") as post:
            with self.assertRaises(nc.ConnectCancelled):
                nc.connect(open_browser=browser, timeout=10, cancel=cancel)
        post.assert_not_called()

    def test_cancel_during_the_swap_keeps_nothing(self):
        cancel = threading.Event()

        def answer(url, **kw):
            if kw["json"]["action"] == "exchange":
                cancel.set()                                 # the user clicks Cancel right now
                return _resp(200, _tokens(access_token="at-late"))
            return _resp(200, {})

        out = self._run(["/notion?code=abc&state={state}"], answer=answer, cancel=cancel)
        self.assertIsInstance(out["error"], nc.ConnectCancelled)
        actions = [c.kwargs["json"]["action"] for c in out["post"].call_args_list]
        self.assertEqual(actions, ["exchange", "revoke"])
        self.assertEqual(out["post"].call_args_list[1].kwargs["json"]["token"], "at-late")

    def test_exchange_errors_come_through_connect(self):
        for status, body, code in ((400, {"error": "invalid_grant"}, "expired"),
                                   (503, {"error": "server_misconfigured"}, "server"),
                                   (502, {"error": "notion_failed"}, "server")):
            out = self._run(["/notion?code=abc&state={state}"], answer=_resp(status, body))
            self.assertEqual(out["error"].code, code)

    def test_offline_during_the_swap(self):
        out = self._run(["/notion?code=abc&state={state}"],
                        answer=Offline("HTTPSConnectionPool(host='x')"))
        self.assertEqual(out["error"].code, "offline")
        self.assertEqual(str(out["error"]), nc.MESSAGES["offline"])

    def test_no_secret_reaches_the_log(self):
        with _captured_logs() as lines:
            self._run(["/notion?code=SECRET-CODE-XYZ&state={state}"],
                      answer=_resp(200, _tokens(access_token="SECRET-ACCESS-XYZ",
                                                refresh_token="SECRET-REFRESH-XYZ")))
            self._run(["/notion?code=SECRET-CODE-XYZ&state={state}"],
                      answer=_resp(400, {"error": "invalid_grant", "message": "SECRET-CODE-XYZ"}))
        text = "\n".join(lines)
        for word in SECRET_WORDS:
            self.assertNotIn(word, text)

    def test_the_whole_round_trip_through_a_relay_like_the_function(self):
        """The browser follows Notion's redirect to the function, whose bare
        302 goes to the listener on 127.0.0.1 (the port rides in the state)."""
        import http.server
        import urllib.request

        class Relay(http.server.BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                q = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
                parsed = nc.parse_state(q.get("state", [""])[0])
                if not parsed or "code" not in q:
                    self.send_response(400)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                where = f"http://127.0.0.1:{parsed[1]}{nc.CALLBACK_PATH}?" + urllib.parse.urlencode(
                    {"code": q["code"][0], "state": q["state"][0]})
                self.send_response(302)
                self.send_header("Location", where)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def log_message(self, *a):
                return

        relay = http.server.HTTPServer(("127.0.0.1", 0), Relay)
        threading.Thread(target=relay.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        self.addCleanup(relay.server_close)
        self.addCleanup(relay.shutdown)
        seen = {}

        def browser(url):
            state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]

            def go():
                opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
                with opener.open(f"http://127.0.0.1:{relay.server_address[1]}/?code=the-code&state={state}",
                                 timeout=5) as r:
                    seen["status"], seen["body"] = r.status, r.read().decode("utf-8")
            t = threading.Thread(target=go, daemon=True)
            seen["thread"] = t
            t.start()
            return True

        with patch.object(nc.requests, "post", return_value=_resp(200, self.TOK)) as post:
            tok = nc.connect(open_browser=browser, timeout=10)
        seen["thread"].join(5)
        self.assertEqual(tok["access_token"], "at-new")
        self.assertEqual(post.call_args.kwargs["json"]["code"], "the-code")
        self.assertEqual((seen["status"], seen["body"]), (200, nc.landing_html("ok")))

    def test_a_late_failing_callback_does_not_break_the_flow(self):
        def bad(*a):
            raise RuntimeError("ui gone")

        def browser(url):
            state = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)["state"][0]
            port = nc.parse_state(state)[1]

            def hit():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", f"/notion?code=abc&state={state}")
                conn.getresponse().read()
                conn.close()
            threading.Thread(target=hit, daemon=True).start()
            return True
        with patch.object(nc.requests, "post", return_value=_resp(200, self.TOK)):
            tok = nc.connect(open_browser=browser, timeout=5, on_url=bad, on_code=bad)
        self.assertEqual(tok["access_token"], "at-new")


class TestLoopbackServer(unittest.TestCase):
    def test_no_other_socket_can_take_the_listening_port(self):
        server = nc._LoopbackServer(("127.0.0.1", 0), nc._Callback)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with self.assertRaises(OSError):
            other.bind(("127.0.0.1", port))

    def test_the_first_valid_answer_wins(self):
        server = nc._LoopbackServer(("127.0.0.1", 0), nc._Callback)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        state = nc.build_state("abcdefghijklmnop", port)
        server.timeout = 2
        server.result = None
        server.done = threading.Event()
        server.lock = threading.Lock()
        server.expected_state = state
        statuses = []

        def hit(code):
            conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
            conn.request("GET", f"/notion?code={code}&state={state}")
            statuses.append(conn.getresponse().status)
            conn.close()

        for code in ("first", "second"):
            t = threading.Thread(target=hit, args=(code,))
            t.start()
            server.handle_request()
            t.join(5)
            self.assertTrue(server.done.wait(2))
        self.assertEqual(statuses, [200, 200])             # the late one still gets its page
        self.assertEqual(server.result, {"code": "first"})

    def test_it_only_listens_on_this_computer(self):
        server = nc._LoopbackServer(("127.0.0.1", 0), nc._Callback)
        self.addCleanup(server.server_close)
        self.assertEqual(server.server_address[0], "127.0.0.1")


# ── the edge function ────────────────────────────────────────────────────────
class TestExchange(NetCase):
    def test_a_full_answer(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(
            workspace_icon="https://x/i.png", duplicated_template_id="tpl")))
        tok = nc.exchange("the-code")
        self.assertEqual(set(tok), {"access_token", "refresh_token", "workspace_id", "workspace_name",
                                    "workspace_icon", "bot_id", "duplicated_template_id"})
        self.assertTrue(all(isinstance(v, str) for v in tok.values()))
        self.assertEqual(tok["duplicated_template_id"], "tpl")
        call = self.http.calls[0]
        self.assertEqual(call.json, {"action": "exchange", "code": "the-code",
                                     "redirect_uri": nc.REDIRECT_URI})
        self.assertEqual(call.timeout, nc.HTTP_TIMEOUT)
        self.assertNotIn("Authorization", call.headers)     # no secret of ours is sent from here

    def test_missing_and_null_fields_are_empty_strings(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, {"access_token": "at", "workspace_icon": None,
                                                           "workspace_name": None, "bot_id": 5}))
        tok = nc.exchange("c")
        self.assertEqual(tok["refresh_token"], "")
        self.assertEqual(tok["workspace_icon"], "")
        self.assertEqual(tok["workspace_name"], "")
        self.assertEqual(tok["bot_id"], "")

    def test_a_long_workspace_name_is_capped(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(workspace_name=" " + "w" * 500)))
        self.assertEqual(len(nc.exchange("c")["workspace_name"]), 200)

    def test_a_workspace_name_is_text_never_markup(self):
        # The owner of a Notion workspace picks its name: no tags, no control characters.
        hostile = '<img src="file://attacker.example/x/y.png">Acme' + chr(0) + chr(10) + "Inc" + chr(0x2028) + "&Co"
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(workspace_name=hostile)))
        name = nc.exchange("c")["workspace_name"]
        self.assertNotIn("<", name)
        self.assertNotIn(">", name)
        self.assertTrue(all(ord(c) >= 32 and ord(c) not in range(0x7f, 0xa0) for c in name))
        self.assertTrue(name.endswith("Acme Inc &Co"))
        self.assertEqual(nc.clean_name("  Aram's   workspace "), "Aram's workspace")
        self.assertEqual(nc.clean_name(None), "")
        self.assertEqual(nc.clean_name(5), "")

    def test_validate_token_cleans_the_name_too(self):
        self.http.add("GET", "/users/me", _resp(200, {"object": "user", "bot": {"workspace_name": "<b>Acme</b>"}}))
        self.assertEqual(nc.validate_token(TOKEN), {"workspace_name": "b Acme /b"})

    def test_no_code_never_calls_out(self):
        for code in ("", "  ", None):
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.exchange(code)
            self.assertEqual(ctx.exception.code, "other")
        self.assertEqual(self.http.calls, [])

    def test_how_each_failure_reads(self):
        cases = [
            (400, {"error": "invalid_grant"}, "expired"),
            (400, {"error": "invalid_redirect"}, "other"),
            (400, {"error": "weird"}, "other"),
            (400, None, "other"),
            (405, {"error": "method_not_allowed"}, "other"),
            (413, {"error": "too_large"}, "other"),
            (429, {"error": "rate_limited"}, "server"),
            (404, {"error": "not_found"}, "server"),
            (500, None, "server"),
            (502, {"error": "notion_failed"}, "server"),
            (503, {"error": "server_misconfigured"}, "server"),
            (504, {"error": "notion_unreachable"}, "server"),
        ]
        for status, body, code in cases:
            self.http.add("POST", nc.FUNCTION_URL, _resp(status, body))
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.exchange("c")
            self.assertEqual(ctx.exception.code, code, (status, body))
            self.assertEqual(str(ctx.exception), nc.MESSAGES[code])

    def test_an_unreadable_answer_is_a_server_problem(self):
        bad = _resp(200)
        bad.json.side_effect = ValueError("not json")
        for answer in (bad, _resp(200, ["list"]), _resp(200, {"no": "token"}),
                       _resp(200, {"access_token": ""}), _resp(200, {"access_token": 5})):
            self.http.add("POST", nc.FUNCTION_URL, answer)
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.exchange("c")
            self.assertEqual(ctx.exception.code, "server")

    def test_offline_says_offline_with_the_fixed_sentence(self):
        for exc in (Offline("HTTPSConnectionPool(host='x', port=443)"), Offline("read timed out"),
                    Offline("bad cert")):
            self.http.add("POST", nc.FUNCTION_URL, exc)
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.exchange("c")
            self.assertEqual(ctx.exception.code, "offline")
            self.assertEqual(str(ctx.exception), nc.MESSAGES["offline"])

    def test_what_the_server_says_never_reaches_the_message(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(400, {"error": "invalid_grant",
                                                           "message": "SECRET-CODE-XYZ was used"}))
        with self.assertRaises(nc.ConnectError) as ctx:
            nc.exchange("SECRET-CODE-XYZ")
        self.assertNotIn("SECRET", str(ctx.exception))

    def test_nothing_secret_is_logged(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(
            access_token="SECRET-ACCESS-XYZ", refresh_token="SECRET-REFRESH-XYZ")),
            _resp(400, {"error": "invalid_grant", "message": "SECRET-CODE-XYZ"}),
            Offline("SECRET-CODE-XYZ"))
        with _captured_logs() as lines:
            nc.exchange("SECRET-CODE-XYZ")
            for _ in range(2):
                with self.assertRaises(nc.ConnectError):
                    nc.exchange("SECRET-CODE-XYZ")
        text = "\n".join(lines)
        for word in SECRET_WORDS:
            self.assertNotIn(word, text)
        self.assertIn("exchange", text)                    # it does say what happened

    def test_a_new_connection_starts_a_new_refresh_chain(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(access_token="a2", refresh_token="r2")),
                      _resp(200, _tokens(access_token="a3", refresh_token="r3")))
        nc.refresh("r1")
        self.assertEqual(nc.latest_tokens()["access_token"], "a2")
        nc.exchange("c")                                   # connecting again forgets the old chain
        self.assertEqual(nc.latest_tokens(), {})
        nc.refresh("r1")                                   # so r1 is not "already swapped" any more
        self.assertEqual(len(self.http.on("POST", nc.FUNCTION_URL)), 3)


class TestRefresh(NetCase):
    def test_sends_the_refresh_token_and_returns_the_new_pair(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(access_token="a2", refresh_token="r2")))
        tok = nc.refresh("r1")
        self.assertEqual((tok["access_token"], tok["refresh_token"]), ("a2", "r2"))
        self.assertEqual(self.http.calls[0].json, {"action": "refresh", "refresh_token": "r1"})
        self.assertEqual(nc.latest_tokens()["refresh_token"], "r2")

    def test_a_refresh_that_returns_no_new_token_keeps_the_old_one(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(access_token="a2", refresh_token="")))
        self.assertEqual(nc.refresh("r1")["refresh_token"], "r1")

    def test_no_token_means_connect_again_and_no_request(self):
        for t in ("", "  ", None):
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.refresh(t)
            self.assertEqual(ctx.exception.code, "expired")
        self.assertEqual(self.http.calls, [])

    def test_a_token_notion_refuses_is_expired(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(400, {"error": "invalid_grant"}))
        with self.assertRaises(nc.ConnectError) as ctx:
            nc.refresh("r1")
        self.assertEqual(ctx.exception.code, "expired")
        self.assertEqual(nc.latest_tokens(), {})

    def test_trouble_that_is_not_the_users_is_not_expiry(self):
        for answer in (Offline("x"), _resp(503, {"error": "server_misconfigured"}),
                       _resp(504, {"error": "notion_unreachable"})):
            self.http.add("POST", nc.FUNCTION_URL, answer)
            with self.assertRaises(nc.ConnectError) as ctx:
                nc.refresh("r1")
            self.assertIn(ctx.exception.code, ("offline", "server"))

    def test_two_threads_with_the_same_old_token_make_one_request(self):
        started = threading.Event()
        posts = []

        def slow(call):
            posts.append(call.json["refresh_token"])
            started.set()
            _real_sleep(0.3)
            return _resp(200, _tokens(access_token="a2", refresh_token="r2"))

        self.http.add("POST", nc.FUNCTION_URL, slow, _resp(500, {"error": "must_not_be_called"}))
        results = []

        def worker():
            results.append(nc.refresh("r1"))

        t1 = threading.Thread(target=worker)
        t1.start()
        self.assertTrue(started.wait(5))                   # t1 is inside the request now
        t2 = threading.Thread(target=worker)
        t2.start()
        t1.join(5)
        t2.join(5)
        self.assertEqual(posts, ["r1"])
        self.assertEqual(len(results), 2)
        self.assertEqual([r["refresh_token"] for r in results], ["r2", "r2"])
        self.assertEqual(results[0], results[1])

    def test_a_stale_token_gets_the_newest_pair_through_a_whole_chain(self):
        self.http.add("POST", nc.FUNCTION_URL,
                      _resp(200, _tokens(access_token="a2", refresh_token="r2")),
                      _resp(200, _tokens(access_token="a3", refresh_token="r3")))
        nc.refresh("r1")
        nc.refresh("r2")
        self.assertEqual(len(self.http.calls), 2)
        self.assertEqual(nc.refresh("r1")["access_token"], "a3")     # swapped twice ago
        self.assertEqual(nc.refresh("r2")["access_token"], "a3")
        self.assertEqual(len(self.http.calls), 2)                    # no third request

    def test_the_current_token_is_refreshed_for_real(self):
        self.http.add("POST", nc.FUNCTION_URL,
                      _resp(200, _tokens(access_token="a2", refresh_token="r2")),
                      _resp(200, _tokens(access_token="a3", refresh_token="r3")))
        nc.refresh("r1")
        self.assertEqual(nc.refresh("r2")["access_token"], "a3")
        self.assertEqual(len(self.http.calls), 2)

    def test_a_token_that_was_not_rotated_is_not_treated_as_swapped(self):
        self.http.add("POST", nc.FUNCTION_URL,
                      _resp(200, _tokens(access_token="a2", refresh_token="r1")),
                      _resp(200, _tokens(access_token="a3", refresh_token="r1")))
        nc.refresh("r1")
        self.assertEqual(nc.refresh("r1")["access_token"], "a3")     # r1 is still the live token
        self.assertEqual(len(self.http.calls), 2)

    def test_a_caller_can_hold_the_lock_around_refresh_and_its_own_save(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(access_token="a2", refresh_token="r2")))
        done = []

        def go():
            with nc._LOCK:                                 # the sync layer does exactly this
                done.append(nc.refresh("r1"))
        t = threading.Thread(target=go)
        t.start()
        t.join(5)
        self.assertFalse(t.is_alive(), "refresh deadlocked on the caller's own lock")
        self.assertEqual(done[0]["access_token"], "a2")

    def test_the_history_is_bounded(self):
        answers = [_resp(200, _tokens(access_token=f"a{i}", refresh_token=f"r{i}")) for i in range(2, 40)]
        self.http.add("POST", nc.FUNCTION_URL, *answers)
        for i in range(1, 38):
            nc.refresh(f"r{i}")
        self.assertLessEqual(len(nc._SUPERSEDED), nc._SUPERSEDED_KEEP)

    def test_nothing_secret_is_logged(self):
        self.http.add("POST", nc.FUNCTION_URL,
                      _resp(200, _tokens(access_token="SECRET-ACCESS-XYZ", refresh_token="SECRET-REFRESH-XYZ")),
                      _resp(400, {"error": "invalid_grant", "message": "SECRET-REFRESH-XYZ"}))
        with _captured_logs() as lines:
            nc.refresh("SECRET-CODE-XYZ")
            with self.assertRaises(nc.ConnectError):
                nc.refresh("SECRET-REFRESH-XYZ")
        text = "\n".join(lines)
        for word in SECRET_WORDS:
            self.assertNotIn(word, text)


class TestRevoke(NetCase):
    def test_gives_the_access_back(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, {}))
        nc.revoke("the-token")
        self.assertEqual(self.http.calls[0].json, {"action": "revoke", "token": "the-token"})

    def test_a_blank_token_is_a_no_op(self):
        for t in ("", "  ", None):
            self.assertIs(nc.revoke(t), True)               # nothing to give back
        self.assertEqual(self.http.calls, [])

    def test_it_says_whether_the_grant_was_really_given_back(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, {"ok": True}))
        self.assertIs(nc.revoke("the-token"), True)
        for answer in (Offline("x"), _resp(500), _resp(503, {"error": "server_misconfigured"}),
                       _resp(429, {"error": "rate_limited"}), _resp(200, ["junk"])):
            self.http.add("POST", nc.FUNCTION_URL, answer)
            self.assertIs(nc.revoke("the-token"), False)    # the grant is still at Notion

    def test_never_raises(self):
        for answer in (Offline("x"), _resp(500), _resp(503, {"error": "server_misconfigured"}),
                       _resp(200, ["junk"])):
            self.http.add("POST", nc.FUNCTION_URL, answer)
            nc.revoke("the-token")                         # must not raise

    def test_forgets_the_remembered_pair(self):
        self.http.add("POST", nc.FUNCTION_URL, _resp(200, _tokens(access_token="a2", refresh_token="r2")))
        nc.refresh("r1")
        self.assertTrue(nc.latest_tokens())
        nc.revoke("a2")
        self.assertEqual(nc.latest_tokens(), {})


# ── Notion pages ─────────────────────────────────────────────────────────────
class TestValidateToken(NetCase):
    def test_returns_the_workspace_name(self):
        self.http.add("GET", "/users/me", _resp(200, {"object": "user", "type": "bot",
                                                      "bot": {"workspace_name": "  Acme  "}}))
        self.assertEqual(nc.validate_token(TOKEN), {"workspace_name": "Acme"})
        call = self.http.calls[0]
        self.assertEqual(call.headers["Authorization"], f"Bearer {TOKEN}")
        self.assertEqual(call.headers["Notion-Version"], "2026-03-11")
        self.assertEqual(call.timeout, nc.HTTP_TIMEOUT)

    def test_a_bot_without_a_workspace_name(self):
        for body in ({"object": "user"}, {"bot": {}}, {"bot": {"workspace_name": None}}, {"bot": None}):
            self.http.add("GET", "/users/me", _resp(200, body))
            self.assertEqual(nc.validate_token(TOKEN), {"workspace_name": ""})

    def test_a_refused_token_is_an_auth_error(self):
        self.http.add("GET", "/users/me", _resp(401, {"code": "unauthorized", "message": "API token is invalid."}))
        with self.assertRaises(ne.NotionAuthError) as ctx:
            nc.validate_token(TOKEN)
        self.assertEqual(ctx.exception.code, "auth")
        self.assertNotIn("invalid.", str(ctx.exception))

    def test_things_that_cannot_be_a_secret_never_leave_the_computer(self):
        for bad in ("", "   ", None, "ntn with space", "ntn_é", "ntn\nline", "x" * 2001):
            with self.assertRaises(ne.NotionAuthError):
                nc.validate_token(bad)
        self.assertEqual(self.http.calls, [])

    def test_a_pasted_secret_with_stray_spaces_is_trimmed(self):
        self.http.add("GET", "/users/me", _resp(200, {"bot": {}}))
        nc.validate_token(f"  {TOKEN}\n")
        self.assertEqual(self.http.calls[0].headers["Authorization"], f"Bearer {TOKEN}")

    def test_offline_is_not_a_wrong_secret(self):
        self.http.add("GET", "/users/me", Offline("HTTPSConnectionPool(host='api.notion.com')"))
        with self.assertRaises(ne.NotionError) as ctx:
            nc.validate_token(TOKEN)
        self.assertNotIsInstance(ctx.exception, ne.NotionAuthError)
        self.assertEqual(ctx.exception.code, "offline")
        self.assertEqual(str(ctx.exception), ne.OFFLINE_TEXT)

    def test_a_server_error_is_plain(self):
        self.http.add("GET", "/users/me", _resp(500, {"code": "internal_server_error", "message": "RAW BOOM"}))
        with self.assertRaises(ne.NotionError) as ctx:
            nc.validate_token(TOKEN)
        self.assertEqual(ctx.exception.code, "other")
        self.assertNotIn("RAW BOOM", str(ctx.exception))


class TestSearchPages(NetCase):
    def test_the_request(self):
        self.http.add("POST", "/search", _search([]))
        self.assertEqual(nc.search_pages(TOKEN), [])
        body = self.http.calls[0].json
        self.assertEqual(body["filter"], {"property": "object", "value": "page"})
        self.assertEqual(body["sort"], {"direction": "descending", "timestamp": "last_edited_time"})
        self.assertEqual(body["page_size"], 50)
        self.assertNotIn("query", body)
        self.assertNotIn("start_cursor", body)

    def test_a_query_is_passed_on(self):
        self.http.add("POST", "/search", _search([]))
        nc.search_pages(TOKEN, query="  meetings ")
        self.assertEqual(self.http.calls[0].json["query"], "meetings")

    def test_title_from_the_property_of_type_title_whatever_its_name(self):
        self.http.add("POST", "/search", _search([_page(1, "Weekly", prop="title"),
                                                  _page(2, "Row in a database", prop="Name")]))
        titles = [p["title"] for p in nc.search_pages(TOKEN)]
        self.assertEqual(titles, ["Weekly", "Row in a database"])

    def test_title_ignores_other_properties(self):
        page = _page(1, "Real title", prop="Name")
        page["properties"] = {
            "Tags": {"type": "rich_text", "rich_text": [{"plain_text": "not me"}]},
            "Name": page["properties"]["Name"]}
        self.http.add("POST", "/search", _search([page]))
        self.assertEqual(nc.search_pages(TOKEN)[0]["title"], "Real title")

    def test_pieces_are_joined_and_whitespace_is_one_line(self):
        page = _page(1)
        page["properties"]["title"]["title"] = [{"plain_text": "Plan "}, {"plain_text": "for\nQ4\t"},
                                                {"text": {"content": "!"}}]
        self.http.add("POST", "/search", _search([page]))
        self.assertEqual(nc.search_pages(TOKEN)[0]["title"], "Plan for Q4 !")

    def test_empty_missing_or_odd_titles_read_untitled(self):
        odd = _page(4)
        odd["properties"] = None
        no_props = _page(5)
        del no_props["properties"]
        blank = _page(3, "   ")
        self.http.add("POST", "/search", _search([_page(1, ""), _page(2, None), blank, odd, no_props]))
        self.assertEqual([p["title"] for p in nc.search_pages(TOKEN)], ["Untitled"] * 5)

    def test_a_very_long_title_is_capped(self):
        self.http.add("POST", "/search", _search([_page(1, "w" * 500)]))
        self.assertEqual(len(nc.search_pages(TOKEN)[0]["title"]), 200)

    def test_trashed_archived_and_other_objects_are_dropped(self):
        self.http.add("POST", "/search", _search([
            _page(1, "alive"), _page(2, "trash", in_trash=True), _page(3, "old", archived=True),
            {"object": "database", "id": "a" * 32}, "junk", None,
            {"object": "page", "id": "not-an-id", "properties": {}}]))
        self.assertEqual([p["title"] for p in nc.search_pages(TOKEN)], ["alive"])

    def test_fields_of_an_item(self):
        page = _page(7, "Notes", icon={"type": "emoji", "emoji": "\U0001F4DD"})
        child = _page(8, "Child", parent={"type": "page_id", "page_id": "x"},
                      icon={"type": "external", "external": {"url": "https://x"}})
        self.http.add("POST", "/search", _search([page, child]))
        a, b = nc.search_pages(TOKEN)
        self.assertEqual(a, {"id": "00000007-1111-2222-3333-000000000007", "title": "Notes",
                             "emoji": "\U0001F4DD", "url": "https://www.notion.so/Page-7", "top": True})
        self.assertEqual((b["emoji"], b["top"]), ("", False))
        self.assertEqual(set(b), {"id", "title", "emoji", "url", "top"})

    def test_ids_come_back_dashed(self):
        page = _page(1)
        page["id"] = HUB_ID                                 # an undashed id from the API
        self.http.add("POST", "/search", _search([page]))
        self.assertEqual(nc.search_pages(TOKEN)[0]["id"], HUB_DASHED)

    def test_follows_the_cursor(self):
        self.http.add("POST", "/search",
                      _search([_page(1), _page(2)], more=True, cursor="c1"),
                      _search([_page(3)], more=False, cursor=None))
        items = nc.search_pages(TOKEN)
        self.assertEqual([p["title"] for p in items], ["Page"] * 3)
        self.assertEqual(len(items), 3)
        self.assertNotIn("start_cursor", self.http.calls[0].json)
        self.assertEqual(self.http.calls[1].json["start_cursor"], "c1")
        self.assertEqual(len(self.http.calls), 2)

    def test_stops_at_the_limit(self):
        self.http.add("POST", "/search", _search([_page(1), _page(2), _page(3)], more=True, cursor="c1"))
        items = nc.search_pages(TOKEN, limit=2)
        self.assertEqual(len(items), 2)
        self.assertEqual(len(self.http.calls), 1)           # no second request once it has enough
        self.assertEqual(self.http.calls[0].json["page_size"], 2)

    def test_pages_are_never_bigger_than_notion_allows(self):
        self.http.add("POST", "/search", _search([]))
        nc.search_pages(TOKEN, limit=1000)
        self.assertEqual(self.http.calls[0].json["page_size"], 100)

    def test_a_page_of_trash_does_not_end_the_list(self):
        self.http.add("POST", "/search",
                      _search([_page(1, in_trash=True), _page(2, archived=True)], more=True, cursor="c1"),
                      _search([_page(3, "found")]))
        self.assertEqual([p["title"] for p in nc.search_pages(TOKEN)], ["found"])

    def test_a_cursor_without_has_more_is_not_followed(self):
        self.http.add("POST", "/search", _search([_page(1)], more=False, cursor="c1"))
        nc.search_pages(TOKEN)
        self.assertEqual(len(self.http.calls), 1)

    def test_it_cannot_loop_forever(self):
        counter = iter(range(1, 10_000))
        self.http.add("POST", "/search", lambda call: _search([_page(next(counter))], more=True, cursor="more"))
        items = nc.search_pages(TOKEN, limit=10_000)
        self.assertEqual(len(self.http.calls), nc.SEARCH_MAX_REQUESTS)
        self.assertEqual(len(items), nc.SEARCH_MAX_REQUESTS)

    def test_the_same_page_twice_is_listed_once(self):
        self.http.add("POST", "/search", _search([_page(1), _page(1)]))
        self.assertEqual(len(nc.search_pages(TOKEN)), 1)

    def test_errors_are_typed(self):
        self.http.add("POST", "/search", _resp(401, {"code": "unauthorized"}))
        with self.assertRaises(ne.NotionAuthError):
            nc.search_pages(TOKEN)
        self.http.add("POST", "/search", Offline("slow"))
        with self.assertRaises(ne.NotionError) as ctx:
            nc.search_pages(TOKEN)
        self.assertEqual(ctx.exception.code, "offline")

    def test_a_busy_notion_is_waited_for_once(self):
        self.http.add("POST", "/search", _resp(429, {"code": "rate_limited"}, {"Retry-After": "1"}), _search([_page(1)]))
        self.assertEqual(len(nc.search_pages(TOKEN)), 1)
        self.assertEqual(len(self.http.calls), 2)


class TestCreateHub(NetCase):
    def test_creates_a_private_page_at_the_top_of_the_workspace(self):
        self.http.add("POST", "/pages", _resp(200, {"id": HUB_ID, "url": "https://www.notion.so/Gusan-meetings-x"}))
        hub = nc.create_hub(TOKEN)
        self.assertEqual(hub, {"id": HUB_DASHED, "title": "Gusan meetings",
                               "url": "https://www.notion.so/Gusan-meetings-x"})
        body = self.http.calls[0].json
        self.assertEqual(body["parent"], {"type": "workspace", "workspace": True})
        self.assertEqual(body["properties"], {"title": {"title": [
            {"type": "text", "text": {"content": "Gusan meetings"}}]}})
        self.assertEqual(len(body["children"]), 1)
        self.assertEqual(body["children"][0]["type"], "paragraph")
        self.assertEqual(body["children"][0]["paragraph"]["rich_text"][0]["text"]["content"],
                         "Meeting notes sent from Gusan land here, one page per meeting.")

    def test_a_custom_title(self):
        self.http.add("POST", "/pages", _resp(200, {"id": HUB_ID}))
        hub = nc.create_hub(TOKEN, title="Notes")
        self.assertEqual(hub["title"], "Notes")
        self.assertEqual(hub["url"], "")
        self.assertEqual(self.http.calls[0].json["properties"]["title"]["title"][0]["text"]["content"], "Notes")

    def test_a_refused_parent_shape_is_tried_once_in_the_short_form(self):
        self.http.add("POST", "/pages",
                      _resp(400, {"code": "validation_error", "message": "body.parent.type should be..."}),
                      _resp(200, {"id": HUB_ID, "url": "u"}))
        self.assertEqual(nc.create_hub(TOKEN)["id"], HUB_DASHED)
        first, second = [c.json["parent"] for c in self.http.calls]
        self.assertEqual(first, {"type": "workspace", "workspace": True})
        self.assertEqual(second, {"workspace": True})
        self.assertEqual(len(self.http.calls), 2)

    def test_two_refusals_raise_a_plain_error(self):
        self.http.add("POST", "/pages", _resp(400, {"code": "validation_error", "message": "RAW PARENT TEXT"}))
        with self.assertRaises(ne.NotionError) as ctx:
            nc.create_hub(TOKEN)
        self.assertEqual(len(self.http.calls), 2)
        self.assertNotIn("RAW PARENT TEXT", str(ctx.exception))

    def test_other_failures_are_not_retried(self):
        for status, cls in ((401, ne.NotionAuthError), (403, ne.NotionError), (404, ne.NotionNotFoundError),
                            (500, ne.NotionError)):
            self.http.calls.clear()
            self.http.add("POST", "/pages", _resp(status, {"code": "x"}))
            with self.assertRaises(cls):
                nc.create_hub(TOKEN)
            self.assertEqual(len(self.http.calls), 1, status)

    def test_an_answer_without_an_id_is_an_error(self):
        self.http.add("POST", "/pages", _resp(200, {"object": "page"}))
        with self.assertRaises(ne.NotionError):
            nc.create_hub(TOKEN)


class TestChooseDestination(NetCase):
    TPL = "fedcba9876543210fedcba9876543210"
    TPL_DASHED = "fedcba98-7654-3210-fedc-ba9876543210"

    def test_the_template_notion_made_comes_first(self):
        self.http.add("GET", f"/pages/{self.TPL_DASHED}", _resp(200, _page(1, "Gusan template")))
        dest = nc.choose_destination(TOKEN, self.TPL)
        self.assertEqual(dest, {"id": self.TPL_DASHED, "title": "Gusan template", "kind": "template"})
        self.assertEqual(len(self.http.calls), 1)           # no hub made when the template is there

    def test_a_template_that_is_gone_falls_through_to_the_hub(self):
        self.http.add("GET", f"/pages/{self.TPL_DASHED}", _resp(404, {"code": "object_not_found"}))
        self.http.add("POST", "/pages", _resp(200, {"id": HUB_ID, "url": "u"}))
        dest = nc.choose_destination(TOKEN, self.TPL)
        self.assertEqual(dest, {"id": HUB_DASHED, "title": "Gusan meetings", "kind": "hub"})

    def test_a_trashed_template_is_not_used(self):
        self.http.add("GET", f"/pages/{self.TPL_DASHED}", _resp(200, _page(1, "T", in_trash=True)))
        self.http.add("POST", "/pages", _resp(200, {"id": HUB_ID}))
        self.assertEqual(nc.choose_destination(TOKEN, self.TPL)["kind"], "hub")

    def test_without_a_template_the_hub_is_made(self):
        self.http.add("POST", "/pages", _resp(200, {"id": HUB_ID, "url": "u"}))
        self.assertEqual(nc.choose_destination(TOKEN), {"id": HUB_DASHED, "title": "Gusan meetings", "kind": "hub"})
        self.assertEqual([c.method for c in self.http.calls], ["POST"])

    def test_a_garbage_template_id_is_ignored(self):
        self.http.add("POST", "/pages", _resp(200, {"id": HUB_ID}))
        self.assertEqual(nc.choose_destination(TOKEN, "not an id")["kind"], "hub")

    def test_no_hub_falls_back_to_the_latest_shared_page(self):
        self.http.add("POST", "/pages", _resp(403, {"code": "restricted_resource"}))
        self.http.add("POST", "/search", _search([_page(5, "Latest"), _page(6, "Older")]))
        dest = nc.choose_destination(TOKEN)
        self.assertEqual(dest, {"id": "00000005-1111-2222-3333-000000000005", "title": "Latest", "kind": "page"})
        self.assertEqual(self.http.on("POST", "/search")[0].json["page_size"], 1)

    def test_a_hub_that_fails_for_its_own_reason_still_falls_back(self):
        for status in (400, 404, 409, 500):
            self.http.calls.clear()
            self.http.add("POST", "/pages", _resp(status, {"code": "x"}))
            self.http.add("POST", "/search", _search([_page(5, "Latest")]))
            self.assertEqual(nc.choose_destination(TOKEN)["kind"], "page", status)

    def test_nothing_anywhere_is_none(self):
        self.http.add("POST", "/pages", _resp(403, {"code": "restricted_resource"}))
        self.http.add("POST", "/search", _search([]))
        self.assertIsNone(nc.choose_destination(TOKEN))

    def test_a_refused_token_stops_the_chain(self):
        self.http.add("POST", "/pages", _resp(401, {"code": "unauthorized"}))
        with self.assertRaises(ne.NotionAuthError):
            nc.choose_destination(TOKEN)
        self.assertEqual(self.http.on("POST", "/search"), [])

    def test_a_refused_template_token_stops_the_chain_too(self):
        self.http.add("GET", f"/pages/{self.TPL_DASHED}", _resp(401, {"code": "unauthorized"}))
        with self.assertRaises(ne.NotionAuthError):
            nc.choose_destination(TOKEN, self.TPL)
        self.assertEqual(len(self.http.calls), 1)

    def test_no_internet_stops_the_chain(self):
        self.http.add("POST", "/pages", Offline("x"))
        with self.assertRaises(ne.NotionError) as ctx:
            nc.choose_destination(TOKEN)
        self.assertEqual(ctx.exception.code, "offline")
        self.assertEqual(self.http.on("POST", "/search"), [])

    def test_a_busy_notion_stops_the_chain(self):
        self.http.add("POST", "/pages", _resp(429, {"code": "rate_limited"}, {"Retry-After": "60"}))
        with self.assertRaises(ne.NotionError) as ctx:
            nc.choose_destination(TOKEN)
        self.assertEqual(ctx.exception.code, "rate")
        self.assertEqual(self.http.on("POST", "/search"), [])

    def test_a_failing_search_raises(self):
        self.http.add("POST", "/pages", _resp(403, {"code": "x"}))
        self.http.add("POST", "/search", _resp(500, {"code": "x"}))
        with self.assertRaises(ne.NotionError):
            nc.choose_destination(TOKEN)


class TestPageAlive(NetCase):
    def test_a_page_we_can_reach(self):
        self.http.add("GET", f"/pages/{HUB_DASHED}", _resp(200, _page(1)))
        self.assertTrue(nc.page_alive(TOKEN, HUB_ID))
        self.assertTrue(nc.page_alive(TOKEN, f"https://www.notion.so/My-Page-{HUB_ID}"))

    def test_gone_or_not_shared(self):
        for answer in (_resp(404, {"code": "object_not_found"}), _resp(403, {"code": "restricted_resource"}),
                       _resp(200, _page(1, in_trash=True)), _resp(200, _page(1, archived=True))):
            self.http.add("GET", f"/pages/{HUB_DASHED}", answer)
            self.assertFalse(nc.page_alive(TOKEN, HUB_ID))

    def test_trouble_that_says_nothing_about_the_page_raises(self):
        self.http.add("GET", f"/pages/{HUB_DASHED}", _resp(401, {"code": "unauthorized"}))
        with self.assertRaises(ne.NotionAuthError):
            nc.page_alive(TOKEN, HUB_ID)
        self.http.add("GET", f"/pages/{HUB_DASHED}", Offline("x"))
        with self.assertRaises(ne.NotionError) as ctx:
            nc.page_alive(TOKEN, HUB_ID)
        self.assertEqual(ctx.exception.code, "offline")
        self.http.add("GET", f"/pages/{HUB_DASHED}", _resp(500, {"code": "x"}))
        with self.assertRaises(ne.NotionError):
            nc.page_alive(TOKEN, HUB_ID)

    def test_a_garbage_id_is_not_a_page_and_costs_no_request(self):
        for bad in ("", None, "nope"):
            self.assertFalse(nc.page_alive(TOKEN, bad))
        self.assertEqual(self.http.calls, [])


class TestNothingSecretIsLogged(NetCase):
    def test_a_token_never_appears_in_a_log_line_or_a_message(self):
        secret = "ntn_SECRET-TOKEN-XYZ"
        self.http.add("GET", "/users/me", _resp(401, {"code": "unauthorized", "message": secret}))
        self.http.add("POST", "/search", Offline(f"Authorization: Bearer {secret}"))
        self.http.add("POST", "/pages", _resp(500, {"code": "internal_server_error", "message": secret}))
        messages = []
        with _captured_logs() as lines:
            for call in (lambda: nc.validate_token(secret), lambda: nc.search_pages(secret),
                         lambda: nc.create_hub(secret)):
                try:
                    call()
                except ne.NotionError as e:
                    messages.append(str(e))
        self.assertEqual(len(messages), 3)
        for text in messages + lines:
            self.assertNotIn("SECRET-TOKEN-XYZ", text)


ROOT = Path(__file__).resolve().parent.parent
EDGE_FUNCTION = ROOT / "supabase" / "functions" / "notion-oauth" / "index.ts"


class TestEdgeFunctionStaysInStep(unittest.TestCase):
    """The client and supabase/functions/notion-oauth/index.ts must agree on a few
    literals; edited alone, one side would only fail in a live Connect (a 400
    invalid_state, a relay to the wrong path). These read the TypeScript source."""

    def setUp(self):
        if not EDGE_FUNCTION.exists():
            self.skipTest("no notion-oauth source here")
        self.src = EDGE_FUNCTION.read_text(encoding="utf-8")

    def test_the_state_shape_is_the_one_the_function_accepts(self):
        m = re.search(r"const STATE_RE = /\^(.+)\$/;", self.src)
        self.assertIsNotNone(m, "STATE_RE not found in index.ts")
        self.assertEqual(m.group(1), nc._STATE_RE.pattern.replace("(", "").replace(")", ""))
        # ...and the port range it enforces after the match
        self.assertIn("port < 1024 || port > 65535", self.src)
        self.assertEqual(nc.parse_state("a" * 16 + ".1023"), None)
        self.assertEqual(nc.parse_state("a" * 16 + ".65536"), None)
        self.assertEqual(nc.parse_state("a" * 16 + ".1024"), ("a" * 16, 1024))

    def test_codes_and_tokens_are_capped_alike(self):
        m = re.search(r"MAX_FIELD_CHARS = (\d+)", self.src)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), nc.MAX_CODE_LEN)

    def test_the_relay_lands_where_the_listener_waits(self):
        self.assertIn("http://127.0.0.1:${port}" + nc.CALLBACK_PATH + "?", self.src)

    def test_the_redirect_is_the_function_itself(self):
        name = re.search(r'FUNCTION_NAME = "([^"]+)"', self.src)
        self.assertIsNotNone(name)
        self.assertEqual(nc.FUNCTION_URL.rsplit("/", 1)[1], name.group(1))
        self.assertTrue(nc.FUNCTION_URL.endswith("/functions/v1/" + name.group(1)))
        self.assertEqual(nc.REDIRECT_URI, nc.FUNCTION_URL)
        base = re.search(r'FALLBACK_BASE = "([^"]+)"', self.src)
        self.assertIsNotNone(base)
        self.assertEqual(nc.FUNCTION_URL, base.group(1) + "/functions/v1/" + name.group(1))

    def test_the_actions_are_the_ones_the_client_posts(self):
        for action in ("exchange", "refresh", "revoke"):
            self.assertIn('"%s"' % action, self.src)


if __name__ == "__main__":
    unittest.main()
