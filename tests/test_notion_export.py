"""Notion export: id parsing, markdown->blocks, and the publish flow (mocked).

Pure stdlib + mock - no network, no Qt. The classes down to TestPublishFlow are the
pre-OAuth tests, unchanged (the default wording is still the old one); the rest
cover typed errors, the OAuth wording, retries, offline and the helpers.
"""
import logging
import unittest
from unittest.mock import MagicMock, patch

import notion_export as ne


class TestExtractPageId(unittest.TestCase):
    DASHED = "01234567-89ab-cdef-0123-456789abcdef"

    def test_url_with_slug(self):
        url = "https://www.notion.so/My-Meetings-0123456789abcdef0123456789abcdef"
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_url_with_query(self):
        url = ("https://www.notion.so/ws/My-Meetings-0123456789abcdef0123456789abcdef"
               "?pvs=4")
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_bare_ids(self):
        self.assertEqual(ne.extract_page_id("0123456789abcdef0123456789abcdef"),
                         self.DASHED)
        self.assertEqual(ne.extract_page_id(self.DASHED), self.DASHED)

    def test_last_id_wins(self):
        # Workspace URLs can carry more than one 32-hex run; the page id is last.
        url = ("https://www.notion.so/aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa/"
               "Page-0123456789abcdef0123456789abcdef")
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_fragment_block_anchor_resolves_to_page_id(self):
        # A URL copied after following an anchor carries the BLOCK id in the
        # fragment; the page id (before the #) must win.
        url = ("https://www.notion.so/My-Meetings-0123456789abcdef0123456789abcdef"
               "#a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6")
        self.assertEqual(ne.extract_page_id(url), self.DASHED)

    def test_garbage(self):
        self.assertIsNone(ne.extract_page_id("not a notion link"))
        self.assertIsNone(ne.extract_page_id(""))
        self.assertIsNone(ne.extract_page_id(None))


class TestMarkdownToBlocks(unittest.TestCase):
    def test_headings_bullets_todos(self):
        md = "## Summary\nThe team met.\n### Details\n- point one\n- [ ] task A\n- [x] done B\n1. first"
        blocks = ne.markdown_to_blocks(md)
        kinds = [b["type"] for b in blocks]
        self.assertEqual(kinds, ["heading_2", "paragraph", "heading_3",
                                 "bulleted_list_item", "to_do", "to_do",
                                 "numbered_list_item"])
        self.assertFalse(blocks[4]["to_do"]["checked"])
        self.assertTrue(blocks[5]["to_do"]["checked"])

    def test_long_text_chunked_under_notion_cap(self):
        blocks = ne.markdown_to_blocks("x" * 4000)
        rich = blocks[0]["paragraph"]["rich_text"]
        self.assertGreater(len(rich), 1)
        self.assertTrue(all(len(r["text"]["content"]) <= ne.MAX_RICH_LEN for r in rich))

    def test_nothing_dropped(self):
        md = "weird |table| line\n> quote"
        blocks = ne.markdown_to_blocks(md)
        self.assertEqual(len(blocks), 2)     # both survive as paragraphs

    def test_empty(self):
        self.assertEqual(ne.markdown_to_blocks(""), [])
        self.assertEqual(ne.markdown_to_blocks(None), [])


class TestPublishFlow(unittest.TestCase):
    TOKEN = "ntn_test"
    PAGE = "0123456789abcdef0123456789abcdef"

    def _resp(self, status=200, body=None):
        r = MagicMock(status_code=status)
        r.json.return_value = body or {"id": "new-page-id",
                                       "url": "https://notion.so/new-page"}
        return r

    def test_small_page_single_request(self):
        with patch.object(ne.requests, "post", return_value=self._resp()) as post, \
             patch.object(ne.requests, "patch") as patch_req:
            url = ne.publish_meeting(self.TOKEN, self.PAGE, "Standup",
                                     "## Summary\nWe met.")
        self.assertEqual(url, "https://notion.so/new-page")
        post.assert_called_once()
        patch_req.assert_not_called()
        body = post.call_args[1]["json"]
        self.assertEqual(body["parent"]["page_id"],
                         ne.extract_page_id(self.PAGE))
        self.assertLessEqual(len(body["children"]), ne.MAX_CHILDREN_PER_REQ)

    def test_long_transcript_batches(self):
        transcript = "\n".join(f"Speaker 1: line {i}" for i in range(250))
        with patch.object(ne.requests, "post", return_value=self._resp()) as post, \
             patch.object(ne.requests, "patch", return_value=self._resp()) as patch_req, \
             patch.object(ne.time, "sleep"):
            ne.publish_meeting(self.TOKEN, self.PAGE, "Long", "## S\nnotes",
                               transcript=transcript)
        # 3 notes blocks + 251 transcript blocks = 254 -> 100 create + 2 appends
        self.assertEqual(post.call_count, 1)
        self.assertEqual(patch_req.call_count, 2)
        for c in patch_req.call_args_list:
            self.assertLessEqual(len(c[1]["json"]["children"]),
                                 ne.MAX_CHILDREN_PER_REQ)

    def test_401_explains_token(self):
        with patch.object(ne.requests, "post", return_value=self._resp(401)):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
        self.assertIn("token", str(ctx.exception).lower())

    def test_404_explains_sharing(self):
        # The classic Notion gotcha: page exists but wasn't shared with the
        # integration - Notion returns 404, users think the id is wrong.
        with patch.object(ne.requests, "post", return_value=self._resp(404)):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
        self.assertIn("Connections", str(ctx.exception))

    def test_missing_config(self):
        with self.assertRaises(ne.NotionError):
            ne.publish_meeting("", self.PAGE, "T", "notes")
        with self.assertRaises(ne.NotionError):
            ne.publish_meeting(self.TOKEN, "garbage", "T", "notes")


class Offline(Exception):
    """Stands in for requests' errors: tests/test_core replaces requests with a
    stub that has no exception classes, so a test patches RequestException
    with this one instead of raising requests.ConnectionError."""


def _resp(status=200, body=None, headers=None):
    r = MagicMock(status_code=status)
    r.json.return_value = {} if body is None else body
    r.headers = headers or {}
    return r


class TestNotionErrorShape(unittest.TestCase):
    def test_a_plain_message_still_works(self):
        e = ne.NotionError("text")
        self.assertEqual(str(e), "text")
        self.assertEqual((e.code, e.status, e.retry_after, e.url), ("other", None, None, ""))

    def test_the_subclasses_carry_their_defaults(self):
        auth = ne.NotionAuthError("x")
        self.assertEqual((auth.code, auth.status), ("auth", 401))
        gone = ne.NotionNotFoundError("x")
        self.assertEqual((gone.code, gone.status), ("no_access", 404))
        self.assertIsInstance(auth, ne.NotionError)
        self.assertIsInstance(gone, ne.NotionError)

    def test_attributes_can_be_set(self):
        e = ne.NotionError("x", status=429, code="rate", retry_after=3.5, url="u")
        self.assertEqual((e.status, e.code, e.retry_after, e.url), (429, "rate", 3.5, "u"))
        self.assertEqual(ne.NotionAuthError("x", code="other").code, "other")

    def test_one_error_does_not_change_another(self):
        ne.NotionError("a", code="rate", url="u")
        self.assertEqual((ne.NotionError("b").code, ne.NotionError("b").url), ("other", ""))


class TestTypedFailures(unittest.TestCase):
    TOKEN = "ntn_test"
    PAGE = "0123456789abcdef0123456789abcdef"

    def _fail(self, status, body=None, headers=None, **kw):
        with patch.object(ne.requests, "post", return_value=_resp(status, body, headers)), \
             patch.object(ne.time, "sleep"):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes", **kw)
        return ctx.exception

    def test_401_is_an_auth_error(self):
        e = self._fail(401)
        self.assertIsInstance(e, ne.NotionAuthError)
        self.assertEqual((e.code, e.status), ("auth", 401))

    def test_404_is_not_found(self):
        e = self._fail(404)
        self.assertIsInstance(e, ne.NotionNotFoundError)
        self.assertEqual((e.code, e.status), ("no_access", 404))

    def test_403_is_no_access_but_not_a_missing_page(self):
        e = self._fail(403, {"code": "restricted_resource"})
        self.assertNotIsInstance(e, ne.NotionNotFoundError)
        self.assertEqual((e.code, e.status), ("no_access", 403))

    def test_429_is_rate_with_the_wait_notion_asked_for(self):
        e = self._fail(429, {"code": "rate_limited"}, {"Retry-After": "45"})
        self.assertEqual((e.code, e.status, e.retry_after), ("rate", 429, 45.0))

    def test_everything_else_is_other(self):
        for status in (400, 409, 500, 502, 503):
            e = self._fail(status, {"code": "x", "message": "m"})
            self.assertEqual((e.code, e.status), ("other", status))

    def test_an_unreadable_body_changes_nothing(self):
        bad = _resp(500)
        bad.json.side_effect = ValueError("not json")
        with patch.object(ne.requests, "post", return_value=bad):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
        self.assertEqual(ctx.exception.code, "other")

    def test_missing_pieces_have_their_own_kinds(self):
        with self.assertRaises(ne.NotionAuthError):
            ne.publish_meeting("", self.PAGE, "T", "notes")
        with self.assertRaises(ne.NotionError) as ctx:
            ne.publish_meeting(self.TOKEN, "garbage", "T", "notes")
        self.assertEqual(ctx.exception.code, "no_access")
        with self.assertRaises(ne.NotionError) as ctx:
            ne.publish_meeting(self.TOKEN, self.PAGE, "T", "")
        self.assertEqual(ctx.exception.code, "other")
        self.assertIn("Nothing to send", str(ctx.exception))

    def test_nothing_the_provider_said_reaches_the_message(self):
        body = {"code": "validation_error", "message": "body.children[0] RAW PROVIDER TEXT"}
        for oauth in (False, True):
            for status in (400, 403, 404, 409, 429, 500):
                e = self._fail(status, body, oauth=oauth)
                self.assertNotIn("RAW PROVIDER", str(e), (status, oauth))
                self.assertNotIn("validation_error", str(e))


class TestWordingByMode(unittest.TestCase):
    TOKEN = "ntn_test"
    PAGE = "0123456789abcdef0123456789abcdef"

    def _fail(self, status, **kw):
        with patch.object(ne.requests, "post", return_value=_resp(status)):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes", **kw)
        return str(ctx.exception)

    def test_the_pasted_secret_path_keeps_its_wording(self):
        self.assertIn("integration secret", self._fail(401))
        self.assertIn("Connections", self._fail(404))
        self.assertIn("Insert content", self._fail(403))

    def test_oauth_never_mentions_a_secret_a_token_or_connections(self):
        for status in (401, 403, 404, 429, 500):
            text = self._fail(status, oauth=True).lower()
            for word in ("secret", "token", "connections", "integration", "notion.so"):
                self.assertNotIn(word, text, (status, word))

    def test_oauth_sentences(self):
        self.assertEqual(self._fail(401, oauth=True), "Notion needs to be connected again.")
        self.assertEqual(self._fail(404, oauth=True), "Gusan can't reach that Notion page anymore.")
        self.assertEqual(self._fail(403, oauth=True), "Notion won't let Gusan add pages there.")

    def test_oauth_with_no_token(self):
        with self.assertRaises(ne.NotionAuthError) as ctx:
            ne.publish_meeting("", self.PAGE, "T", "notes", oauth=True)
        self.assertEqual(str(ctx.exception), "Notion needs to be connected again.")
        with self.assertRaises(ne.NotionAuthError) as ctx:
            ne.publish_meeting("", self.PAGE, "T", "notes")
        self.assertIn("integration token", str(ctx.exception))

    def test_oauth_does_not_change_what_is_sent(self):
        with patch.object(ne.requests, "post", return_value=_resp(200, {"id": "n", "url": "u"})) as post:
            ne.publish_meeting(self.TOKEN, self.PAGE, "Standup", "## S\nnotes", oauth=True)
        body = post.call_args[1]["json"]
        self.assertEqual(body["parent"], {"page_id": ne.extract_page_id(self.PAGE)})
        self.assertEqual(body["properties"]["title"]["title"][0]["text"]["content"], "Standup")


class TestOffline(unittest.TestCase):
    TOKEN = "ntn_test"
    PAGE = "0123456789abcdef0123456789abcdef"

    def setUp(self):
        patcher = patch.object(ne.requests, "RequestException", Offline)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_no_internet_is_one_fixed_sentence(self):
        for exc in (Offline("HTTPSConnectionPool(host='api.notion.com', port=443): "
                            "Max retries exceeded with url: /v1/pages (Caused by ...)"),
                    Offline("Read timed out. (read timeout=30)"),
                    Offline("certificate verify failed")):
            with patch.object(ne.requests, "post", side_effect=exc):
                with self.assertRaises(ne.NotionError) as ctx:
                    ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
            self.assertEqual(str(ctx.exception), "Couldn't reach Notion. Check your internet.")
            self.assertEqual(ctx.exception.code, "offline")
            self.assertIsNone(ctx.exception.status)

    def test_it_is_logged_without_the_exception_text(self):
        lines = []

        class Grab(logging.Handler):
            def emit(self, record):
                lines.append(self.format(record))

        handler = Grab(level=logging.DEBUG)
        log = logging.getLogger("transcribe.notion")
        old = log.level
        log.addHandler(handler)
        log.setLevel(logging.DEBUG)
        try:
            with patch.object(ne.requests, "post", side_effect=Offline("SECRET-HOST-DETAIL")):
                with self.assertRaises(ne.NotionError):
                    ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
        finally:
            log.removeHandler(handler)
            log.setLevel(old)
        self.assertTrue(lines)
        self.assertNotIn("SECRET-HOST-DETAIL", "\n".join(lines))
        self.assertNotIn(self.TOKEN, "\n".join(lines))


class TestRetryAfter(unittest.TestCase):
    TOKEN = "ntn_test"
    PAGE = "0123456789abcdef0123456789abcdef"
    OK = {"id": "new", "url": "https://notion.so/new"}

    def _publish(self, answers):
        with patch.object(ne.requests, "post", side_effect=answers) as post, \
             patch.object(ne.time, "sleep") as sleep:
            try:
                url = ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes")
            except ne.NotionError as e:
                url = e
        return url, post, sleep

    def test_a_short_wait_is_waited_once_and_then_it_works(self):
        url, post, sleep = self._publish([_resp(429, {}, {"Retry-After": "2"}), _resp(200, self.OK)])
        self.assertEqual(url, "https://notion.so/new")
        self.assertEqual(post.call_count, 2)
        sleep.assert_called_once_with(2.0)

    def test_529_is_treated_the_same(self):
        url, post, sleep = self._publish([_resp(529, {}, {"Retry-After": "1"}), _resp(200, self.OK)])
        self.assertEqual(url, "https://notion.so/new")
        self.assertEqual(post.call_count, 2)

    def test_a_long_wait_is_reported_not_waited_out(self):
        err, post, sleep = self._publish([_resp(429, {}, {"Retry-After": "21"})])
        self.assertIsInstance(err, ne.NotionError)
        self.assertEqual((err.code, err.retry_after), ("rate", 21.0))
        self.assertEqual(post.call_count, 1)
        sleep.assert_not_called()

    def test_exactly_the_limit_is_still_waited(self):
        url, post, sleep = self._publish([_resp(429, {}, {"Retry-After": "20"}), _resp(200, self.OK)])
        self.assertEqual(url, "https://notion.so/new")
        sleep.assert_called_once_with(20.0)

    def test_it_retries_only_once(self):
        err, post, sleep = self._publish([_resp(429, {}, {"Retry-After": "1"}),
                                          _resp(429, {}, {"Retry-After": "1"}),
                                          _resp(200, self.OK)])
        self.assertEqual(err.code, "rate")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(sleep.call_count, 1)

    def test_no_header_or_a_bad_one_means_no_retry(self):
        for headers in ({}, {"Retry-After": "soon"}, {"Retry-After": ""}):
            err, post, sleep = self._publish([_resp(429, {}, headers)])
            self.assertEqual(err.code, "rate", headers)
            self.assertIsNone(err.retry_after)
            self.assertEqual(post.call_count, 1)
            sleep.assert_not_called()

    def test_other_errors_are_never_retried(self):
        for status in (400, 401, 403, 404, 500, 502):
            err, post, sleep = self._publish([_resp(status)])
            self.assertEqual(post.call_count, 1, status)
            sleep.assert_not_called()

    def test_a_retry_in_the_append_phase(self):
        transcript = "\n".join(f"Speaker 1: line {i}" for i in range(150))
        with patch.object(ne.requests, "post", return_value=_resp(200, self.OK)), \
             patch.object(ne.requests, "patch",
                          side_effect=[_resp(429, {}, {"Retry-After": "1"}), _resp(200, {})]) as patch_req, \
             patch.object(ne.time, "sleep"):
            ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes", transcript=transcript)
        self.assertEqual(patch_req.call_count, 2)


class TestHeadersAndVersion(unittest.TestCase):
    def test_the_version_is_the_one_both_sides_use(self):
        self.assertEqual(ne.NOTION_VERSION, "2026-03-11")

    def test_headers(self):
        with patch.object(ne.requests, "post", return_value=_resp(200, {"id": "n", "url": "u"})) as post:
            ne.publish_meeting("ntn_abc", "0123456789abcdef0123456789abcdef", "T", "notes")
        headers = post.call_args[1]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer ntn_abc")
        self.assertEqual(headers["Notion-Version"], "2026-03-11")
        self.assertEqual(headers["Content-Type"], "application/json")
        self.assertEqual(post.call_args[1]["timeout"], ne.HTTP_TIMEOUT)

    def test_a_token_with_stray_spaces_is_trimmed(self):
        with patch.object(ne.requests, "post", return_value=_resp(200, {"id": "n", "url": "u"})) as post:
            ne.publish_meeting("  ntn_abc \n", "0123456789abcdef0123456789abcdef", "T", "notes")
        self.assertEqual(post.call_args[1]["headers"]["Authorization"], "Bearer ntn_abc")

    def test_the_append_uses_the_same_headers(self):
        transcript = "\n".join(f"Speaker 1: line {i}" for i in range(150))
        with patch.object(ne.requests, "post", return_value=_resp(200, {"id": "n", "url": "u"})), \
             patch.object(ne.requests, "patch", return_value=_resp(200, {})) as patch_req, \
             patch.object(ne.time, "sleep"):
            ne.publish_meeting("ntn_abc", "0123456789abcdef0123456789abcdef", "T", "notes",
                               transcript=transcript)
        headers = patch_req.call_args[1]["headers"]
        self.assertEqual((headers["Authorization"], headers["Notion-Version"]),
                         ("Bearer ntn_abc", "2026-03-11"))


class TestPartialUpload(unittest.TestCase):
    TOKEN = "ntn_test"
    PAGE = "0123456789abcdef0123456789abcdef"
    TRANSCRIPT = "\n".join(f"Speaker 1: line {i}" for i in range(150))

    def setUp(self):
        patcher = patch.object(ne.requests, "RequestException", Offline)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _publish(self, patch_answer, **kw):
        with patch.object(ne.requests, "post",
                          return_value=_resp(200, {"id": "n", "url": "https://notion.so/n"})), \
             patch.object(ne.requests, "patch", side_effect=patch_answer), \
             patch.object(ne.time, "sleep"):
            with self.assertRaises(ne.NotionError) as ctx:
                ne.publish_meeting(self.TOKEN, self.PAGE, "T", "notes", transcript=self.TRANSCRIPT, **kw)
        return ctx.exception

    def test_the_error_says_where_the_page_is(self):
        e = self._publish([_resp(500, {"message": "RAW"})])
        self.assertEqual(e.url, "https://notion.so/n")
        self.assertIn("Page created (https://notion.so/n)", str(e))
        self.assertNotIn("RAW", str(e))
        self.assertEqual(e.status, 500)

    def test_an_expired_token_mid_upload_is_not_an_auth_error(self):
        # A caller that refreshes the token and sends again on NotionAuthError
        # must not do that here: the page already exists.
        e = self._publish([_resp(401)], oauth=True)
        self.assertNotIsInstance(e, ne.NotionAuthError)
        self.assertEqual(e.code, "auth")
        self.assertEqual(e.url, "https://notion.so/n")

    def test_losing_the_connection_mid_upload(self):
        e = self._publish(Offline("SECRET-HOST-DETAIL"))
        self.assertEqual(e.code, "offline")
        self.assertEqual(e.url, "https://notion.so/n")
        self.assertNotIn("SECRET-HOST-DETAIL", str(e))

    def test_a_rate_limit_mid_upload_keeps_the_wait(self):
        e = self._publish([_resp(429, {}, {"Retry-After": "90"})])
        self.assertEqual((e.code, e.retry_after), ("rate", 90.0))


class TestCleanToken(unittest.TestCase):
    PAGE = "0123456789abcdef0123456789abcdef"

    def test_a_good_token_is_trimmed(self):
        self.assertEqual(ne.clean_token("  ntn_abc\n"), "ntn_abc")

    def test_what_cannot_be_a_token_never_leaves_the_computer(self):
        for bad in (None, "", "   ", "ntn abc", "ntn\nabc", "ntn_é", "x" * (ne.MAX_TOKEN_LEN + 1)):
            for oauth in (False, True):
                with self.assertRaises(ne.NotionAuthError):
                    ne.clean_token(bad, oauth=oauth)

    def test_the_wording_follows_the_mode(self):
        for bad in ("", "ntn abc"):
            with self.assertRaises(ne.NotionAuthError) as ctx:
                ne.clean_token(bad, oauth=True)
            self.assertEqual(str(ctx.exception), "Notion needs to be connected again.")
        with self.assertRaises(ne.NotionAuthError) as ctx:
            ne.clean_token("")
        self.assertIn("integration token", str(ctx.exception))
        with self.assertRaises(ne.NotionAuthError) as ctx:
            ne.clean_token("ntn abc")
        self.assertIn("integration secret", str(ctx.exception))

    def test_publish_sends_nothing_with_such_a_token(self):
        with patch.object(ne.requests, "post") as post:
            for bad in ("ntn abc", "ntn_é", "ntn\nabc"):
                with self.assertRaises(ne.NotionAuthError):
                    ne.publish_meeting(bad, self.PAGE, "T", "notes")
        post.assert_not_called()

    def test_call_sends_nothing_with_such_a_token(self):
        with patch.object(ne.requests, "get") as get:
            with self.assertRaises(ne.NotionAuthError):
                ne.call("get", "/users/me", "ntn abc")
        get.assert_not_called()


class TestCall(unittest.TestCase):
    def test_returns_the_parsed_object(self):
        with patch.object(ne.requests, "get", return_value=_resp(200, {"object": "user"})) as get:
            self.assertEqual(ne.call("get", "/users/me", "ntn_abc"), {"object": "user"})
        self.assertEqual(get.call_args[0][0], f"{ne.API_BASE}/users/me")
        self.assertNotIn("json", get.call_args[1])

    def test_sends_a_body(self):
        with patch.object(ne.requests, "post", return_value=_resp(200, {})) as post:
            ne.call("post", "/search", "ntn_abc", json={"query": "x"}, timeout=5)
        self.assertEqual(post.call_args[1]["json"], {"query": "x"})
        self.assertEqual(post.call_args[1]["timeout"], 5)

    def test_an_empty_or_unreadable_body_is_an_empty_object(self):
        odd = _resp(200)
        odd.json.side_effect = ValueError("no json")
        for answer in (odd, _resp(200, ["list"])):
            with patch.object(ne.requests, "get", return_value=answer):
                self.assertEqual(ne.call("get", "/x", "t"), {})

    def test_failures_are_typed_and_oauth_aware(self):
        with patch.object(ne.requests, "get", return_value=_resp(401)):
            with self.assertRaises(ne.NotionAuthError) as ctx:
                ne.call("get", "/x", "t", oauth=True)
        self.assertEqual(str(ctx.exception), "Notion needs to be connected again.")


class TestConnectionState(unittest.TestCase):
    PAGE = "https://www.notion.so/My-Meetings-0123456789abcdef0123456789abcdef"

    def test_is_connected_needs_a_token_and_a_destination(self):
        self.assertTrue(ne.is_connected({"notion_api_key": "ntn_x", "notion_parent_page": self.PAGE}))
        self.assertTrue(ne.is_connected({"notion_api_key": " ntn_x ",
                                         "notion_parent_page": "0123456789abcdef0123456789abcdef"}))
        self.assertFalse(ne.is_connected({"notion_api_key": "ntn_x", "notion_parent_page": ""}))
        self.assertFalse(ne.is_connected({"notion_api_key": "ntn_x", "notion_parent_page": "not a page"}))
        self.assertFalse(ne.is_connected({"notion_api_key": "  ", "notion_parent_page": self.PAGE}))
        self.assertFalse(ne.is_connected({"notion_parent_page": self.PAGE}))
        self.assertFalse(ne.is_connected({}))
        self.assertFalse(ne.is_connected(None))
        self.assertFalse(ne.is_connected({"notion_api_key": None, "notion_parent_page": None}))

    def test_connection_mode(self):
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": "oauth"}), "oauth")
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": "own"}), "own")
        # A setup from before OAuth has a token and no mode: it reads as "own".
        self.assertEqual(ne.connection_mode({"notion_api_key": "t"}), "own")
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": ""}), "own")
        self.assertEqual(ne.connection_mode({"notion_api_key": "t", "notion_auth_mode": "junk"}), "own")

    def test_no_token_is_no_mode(self):
        for cfg in ({}, None, {"notion_auth_mode": "oauth"}, {"notion_api_key": " ", "notion_auth_mode": "own"},
                    {"notion_api_key": None}):
            self.assertEqual(ne.connection_mode(cfg), "", cfg)


if __name__ == "__main__":
    unittest.main()
