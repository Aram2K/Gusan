"""Gusan 2.0: notion_sync, the one routine behind Export > Notion and the automatic
send. Fakes only (publish, refresh, create_hub, update_meta): no network, no real
Notion, no Qt.

Covers what each send writes into the meeting, the token refresh (the new pair is
saved BEFORE the retry), the "Gusan meetings" page made again when it is gone, the
one-send-at-a-time guard, and - against the REAL module, not a sys.modules stand-in -
the two places that call it (main's automatic send, the keys the UI reads)."""
import inspect
import importlib.util
import os
import re
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

try:
    from tests import test_core  # noqa: F401  (importing installs the stubs)
except ImportError:
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    import test_core  # noqa: F401

import main
import meeting_store as ms
import notion_connect
import notion_export
import notion_sync
from notion_connect import ConnectError
from notion_export import NotionAuthError, NotionError, NotionNotFoundError

ROOT = Path(__file__).resolve().parent.parent
HUB = "99999999-9999-9999-9999-999999999999"
PICKED = "11111111-1111-1111-1111-111111111111"
URL = "https://www.notion.so/Weekly-sync-0123456789abcdef0123456789abcdef"
OAUTH = {"notion_api_key": "tok-1", "notion_refresh_token": "ref-1", "notion_auth_mode": "oauth",
         "notion_parent_page": HUB, "notion_hub_page": HUB, "notion_parent_title": "Gusan meetings"}
OWN = {"notion_api_key": "ntn_secret", "notion_auth_mode": "own", "notion_parent_page": PICKED}


class Fakes:
    """publish / refresh / create_hub / update_meta stand-ins that record one shared log."""

    def __init__(self, publishes=None, refreshes=None, hubs=None):
        self.log = []
        self.publishes = list(publishes if publishes is not None else [URL])
        self.refreshes = list(refreshes or [])
        self.hubs = list(hubs or [])
        self.saved = []
        self.meta = []
        self.calls = []

    def _next(self, queue):
        out = queue.pop(0)
        if isinstance(out, Exception):
            raise out
        return out

    def publish(self, token, parent, title, notes, transcript="", meta_line="", *, oauth=False):
        self.log.append("publish")
        self.calls.append({"token": token, "parent": parent, "title": title, "notes": notes,
                           "transcript": transcript, "meta_line": meta_line, "oauth": oauth})
        return self._next(self.publishes)

    def refresh(self, refresh_token):
        self.log.append("refresh")
        self.refreshed = getattr(self, "refreshed", []) + [refresh_token]
        return self._next(self.refreshes)

    def create_hub(self, token):
        self.log.append("create_hub")
        return self._next(self.hubs)

    def persist(self, updates):
        self.log.append("persist")
        self.saved.append(dict(updates))

    def update_meta(self, folder, **fields):
        self.log.append("update_meta")
        self.meta.append(fields)
        return ms.update_meta(folder, **fields)

    def send(self, folder, cfg, **kw):
        return notion_sync.send_meeting(folder, cfg, persist=self.persist, publish=self.publish,
                                        refresh=self.refresh, create_hub=self.create_hub,
                                        update_meta=self.update_meta, **kw)


def tokens(access="tok-2", refresh="ref-2"):
    return {"access_token": access, "refresh_token": refresh, "workspace_name": "W"}


class _Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.folder = str(Path(self._tmp.name) / "20261008_101500")
        os.mkdir(self.folder)
        Path(self.folder, "notes.md").write_text("## Summary\n- Ship Friday", encoding="utf-8")
        Path(self.folder, "transcript.txt").write_text("Anna: We ship Friday.", encoding="utf-8")
        ms.update_meta(self.folder, title="Weekly sync", started="2026-10-08 10:15:00", duration_sec=1500,
                       attendees_list=["Anna", "David"])

    def meta(self):
        return ms.load_meta(self.folder)


# ── a send that works ────────────────────────────────────────────────────────

class TestSuccess(_Case):
    def test_it_publishes_and_writes_the_page_into_the_meeting(self):
        f = Fakes()
        res = f.send(self.folder, OAUTH)
        self.assertEqual(res, {"ok": True, "url": URL, "code": "sent", "sentence": ""})
        meta = self.meta()
        self.assertEqual(meta["notion_url"], URL)
        self.assertEqual(meta["notion_page_id"], "01234567-89ab-cdef-0123-456789abcdef")
        self.assertRegex(meta["notion_sent_at"], r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\dZ$")
        self.assertIs(meta["notion_auto"], False)
        self.assertNotIn("notion_error", meta)
        self.assertEqual(meta["title"], "Weekly sync")                  # the rest of the meta is kept

    def test_what_is_sent(self):
        f = Fakes()
        f.send(self.folder, OAUTH)
        call = f.calls[0]
        self.assertEqual((call["token"], call["parent"], call["title"]), ("tok-1", HUB, "Weekly sync"))
        self.assertEqual(call["notes"], "## Summary\n- Ship Friday")
        self.assertEqual(call["transcript"], "Anna: We ship Friday.")
        self.assertIs(call["oauth"], True)
        self.assertEqual(call["meta_line"], "Thu 8 Oct · 10:15 · 25 min 00 s · Anna, David · Gusan")

    def test_an_automatic_send_is_the_notes_only_and_says_so(self):
        f = Fakes()
        f.send(self.folder, OAUTH, auto=True)
        self.assertEqual(f.calls[0]["transcript"], "")
        self.assertEqual(f.calls[0]["notes"], "## Summary\n- Ship Friday")
        self.assertIs(self.meta()["notion_auto"], True)

    def test_a_pasted_secret_keeps_the_old_wording(self):
        f = Fakes()
        f.send(self.folder, OWN)
        self.assertIs(f.calls[0]["oauth"], False)
        legacy = {"notion_api_key": "ntn_old", "notion_parent_page": PICKED}        # from before OAuth
        f = Fakes()
        f.send(self.folder, legacy)
        self.assertIs(f.calls[0]["oauth"], False)

    def test_a_meeting_without_a_title_gets_a_plain_one(self):
        ms.update_meta(self.folder, title=None)
        f = Fakes()
        f.send(self.folder, OAUTH)
        self.assertEqual(f.calls[0]["title"], "Meeting notes")
        ms.update_meta(self.folder, title="   ")
        f.publishes.append(URL)
        f.send(self.folder, OAUTH)
        self.assertEqual(f.calls[1]["title"], "Meeting notes")

    def test_a_manual_resend_makes_a_new_page_and_replaces_the_link(self):
        ms.update_meta(self.folder, notion_url="https://www.notion.so/old", notion_error="offline")
        f = Fakes(publishes=["https://www.notion.so/New-fedcba9876543210fedcba9876543210"])
        res = f.send(self.folder, OAUTH)
        self.assertTrue(res["ok"])
        meta = self.meta()
        self.assertTrue(meta["notion_url"].endswith("fedcba9876543210fedcba9876543210"))
        self.assertNotIn("notion_error", meta)

    def test_notes_alone_or_transcript_alone_is_enough_for_a_manual_send(self):
        os.remove(Path(self.folder, "notes.md"))
        f = Fakes()
        self.assertTrue(f.send(self.folder, OAUTH)["ok"])
        self.assertEqual(f.calls[0]["notes"], "")

    def test_the_defaults_are_the_real_functions_looked_up_when_called(self):
        with patch.object(notion_export, "publish_meeting", return_value=URL) as pub:
            res = notion_sync.send_meeting(self.folder, OAUTH)
        self.assertTrue(res["ok"])
        self.assertEqual(pub.call_args[0][:3], ("tok-1", HUB, "Weekly sync"))
        self.assertIs(pub.call_args[1]["oauth"], True)


class TestAutoSkips(_Case):
    def test_an_automatic_send_of_a_meeting_already_in_notion_does_nothing(self):
        ms.update_meta(self.folder, notion_url="https://www.notion.so/earlier")
        f = Fakes()
        res = f.send(self.folder, OAUTH, auto=True)
        self.assertEqual((res["ok"], res["code"], res["url"]), (False, "sent", "https://www.notion.so/earlier"))
        self.assertEqual(f.log, [])                                     # nothing sent, nothing written
        self.assertEqual(self.meta()["notion_url"], "https://www.notion.so/earlier")

    def test_nothing_to_send(self):
        os.remove(Path(self.folder, "notes.md"))
        os.remove(Path(self.folder, "transcript.txt"))
        f = Fakes()
        res = f.send(self.folder, OAUTH)
        self.assertEqual((res["ok"], res["code"], res["sentence"]), (False, "other", "Nothing to send yet."))
        self.assertEqual(f.log, [])
        self.assertNotIn("notion_error", self.meta())

    def test_an_automatic_send_needs_notes_even_with_a_transcript(self):
        os.remove(Path(self.folder, "notes.md"))
        f = Fakes()
        self.assertEqual(f.send(self.folder, OAUTH, auto=True)["sentence"], "Nothing to send yet.")
        self.assertEqual(f.log, [])


# ── a send that fails ────────────────────────────────────────────────────────

class TestFailures(_Case):
    CASES = (
        (NotionNotFoundError("gone"), OWN, "no_access", "Couldn't send to Notion. Pick another page in Meeting settings."),
        (NotionError("slow down", status=429, code="rate"), OAUTH, "rate", "Notion is busy. Try again in a minute."),
        (NotionError("net", code="offline"), OAUTH, "offline", "Couldn't reach Notion. Check your internet."),
        (NotionError("boom", status=500), OAUTH, "other", "Couldn't send to Notion."),
        (NotionError("403", status=403, code="no_access"), OAUTH, "no_access",
         "Couldn't send to Notion. Pick another page in Meeting settings."),
        (NotionAuthError("401"), OWN, "auth", "Couldn't send to Notion. Notion rejected the secret."),
        (RuntimeError("a bug, with a secret tok-1 in it"), OAUTH, "other", "Couldn't send to Notion."),
    )

    def test_each_failure_writes_its_code_and_says_one_plain_sentence(self):
        for exc, cfg, code, sentence in self.CASES:
            f = Fakes(publishes=[exc])
            res = f.send(self.folder, cfg)
            self.assertEqual((res["ok"], res["url"], res["code"], res["sentence"]), (False, "", code, sentence), code)
            self.assertEqual(self.meta()["notion_error"], code)
            self.assertNotIn("notion_url", self.meta())
            self.assertNotIn("tok-1", repr(res))

    def test_a_failure_never_raises_and_a_later_success_clears_the_error(self):
        f = Fakes(publishes=[NotionError("net", code="offline"), URL])
        self.assertFalse(f.send(self.folder, OAUTH)["ok"])
        self.assertEqual(self.meta()["notion_error"], "offline")
        self.assertTrue(f.send(self.folder, OAUTH)["ok"])
        self.assertNotIn("notion_error", self.meta())

    def test_a_failure_writes_only_the_error(self):
        f = Fakes(publishes=[NotionError("net", code="offline")])
        f.send(self.folder, OAUTH)
        self.assertEqual(f.meta, [{"notion_error": "offline"}])

    def test_a_page_that_was_made_but_lost_its_tail_keeps_its_link(self):
        f = Fakes(publishes=[NotionError("Page created but part failed", code="offline", url=URL)])
        res = f.send(self.folder, OAUTH, auto=True)
        self.assertEqual((res["ok"], res["url"], res["code"]), (False, URL, "offline"))
        self.assertEqual(self.meta()["notion_url"], URL)             # so an automatic retry cannot make a second page
        self.assertEqual(self.meta()["notion_error"], "offline")

    def test_a_meta_write_that_fails_does_not_break_the_answer(self):
        def boom(folder, **fields):
            raise OSError("disk full")
        res = notion_sync.send_meeting(self.folder, OAUTH, publish=Fakes().publish, update_meta=boom)
        self.assertTrue(res["ok"])

    def test_a_persist_that_fails_does_not_break_the_send(self):
        f = Fakes(publishes=[NotionAuthError("401"), URL], refreshes=[tokens()])

        def boom(updates):
            raise RuntimeError("gui gone")
        res = notion_sync.send_meeting(self.folder, OAUTH, persist=boom, publish=f.publish, refresh=f.refresh,
                                       update_meta=f.update_meta)
        self.assertTrue(res["ok"])

    def test_friendly_maps_every_kind_of_error(self):
        self.assertEqual(notion_sync.friendly(NotionAuthError("x"), "oauth"),
                         ("auth", "Couldn't send to Notion. Reconnect it in Meeting settings."))
        self.assertEqual(notion_sync.friendly(NotionAuthError("x"), "own"),
                         ("auth", "Couldn't send to Notion. Notion rejected the secret."))
        self.assertEqual(notion_sync.friendly(ConnectError("expired"), "oauth")[0], "auth")
        self.assertEqual(notion_sync.friendly(ConnectError("offline"), "oauth")[0], "offline")
        self.assertEqual(notion_sync.friendly(ConnectError("server"), "oauth")[0], "other")
        self.assertEqual(notion_sync.friendly(ValueError("x"), "oauth"), ("other", "Couldn't send to Notion."))
        for code in notion_sync.CODES:
            self.assertIn(code, ("sent", "busy", "auth", "no_access", "rate", "offline", "other"))


# ── the token ────────────────────────────────────────────────────────────────

class TestRefresh(_Case):
    def test_a_refused_token_is_refreshed_and_the_pair_is_saved_before_the_retry(self):
        f = Fakes(publishes=[NotionAuthError("401"), URL], refreshes=[tokens("tok-2", "ref-2")])
        res = f.send(self.folder, OAUTH)
        self.assertTrue(res["ok"])
        self.assertEqual(f.log[:5], ["publish", "refresh", "persist", "publish", "update_meta"])
        self.assertEqual(f.refreshed, ["ref-1"])
        self.assertEqual(f.saved, [{"notion_api_key": "tok-2", "notion_refresh_token": "ref-2"}])
        self.assertEqual([c["token"] for c in f.calls], ["tok-1", "tok-2"])         # the retry uses the new token
        self.assertEqual(self.meta()["notion_url"], URL)

    def test_a_refresh_that_keeps_the_old_refresh_token_keeps_it(self):
        f = Fakes(publishes=[NotionAuthError("401"), URL], refreshes=[{"access_token": "tok-2", "refresh_token": ""}])
        f.send(self.folder, OAUTH)
        self.assertEqual(f.saved, [{"notion_api_key": "tok-2", "notion_refresh_token": "ref-1"}])

    def test_the_refresh_runs_under_the_connection_lock(self):
        held = []

        def refresh(token):
            held.append(notion_connect._LOCK._is_owned())
            return tokens()
        f = Fakes(publishes=[NotionAuthError("401"), URL])
        notion_sync.send_meeting(self.folder, OAUTH, persist=f.persist, publish=f.publish, refresh=refresh,
                                 update_meta=f.update_meta)
        self.assertEqual(held, [True])
        self.assertFalse(notion_connect._LOCK._is_owned())                          # and let go of it

    def test_a_refresh_notion_refuses_for_good_asks_to_connect_again(self):
        f = Fakes(publishes=[NotionAuthError("401")], refreshes=[ConnectError("expired")])
        res = f.send(self.folder, OAUTH)
        self.assertEqual((res["ok"], res["code"]), (False, "auth"))
        self.assertEqual(res["sentence"], "Couldn't send to Notion. Reconnect it in Meeting settings.")
        self.assertEqual(f.saved, [{"notion_reconnect": True}])
        self.assertEqual(self.meta()["notion_error"], "auth")
        self.assertEqual(f.log.count("publish"), 1)                                 # no retry without a token

    def test_a_missing_refresh_token_cannot_refresh(self):
        cfg = dict(OAUTH, notion_refresh_token="")
        f = Fakes(publishes=[NotionAuthError("401")])
        f.refresh = lambda token: notion_connect.refresh(token)                    # the real one: "" is "expired"
        res = notion_sync.send_meeting(self.folder, cfg, persist=f.persist, publish=f.publish, refresh=f.refresh,
                                       update_meta=f.update_meta)
        self.assertEqual(res["code"], "auth")
        self.assertEqual(f.saved, [{"notion_reconnect": True}])

    def test_no_internet_during_the_refresh_is_offline_and_not_a_lost_connection(self):
        f = Fakes(publishes=[NotionAuthError("401")], refreshes=[ConnectError("offline")])
        res = f.send(self.folder, OAUTH)
        self.assertEqual((res["code"], f.saved), ("offline", []))

    def test_a_token_that_is_refused_twice_is_a_lost_connection(self):
        f = Fakes(publishes=[NotionAuthError("401"), NotionAuthError("401 again")], refreshes=[tokens()])
        res = f.send(self.folder, OAUTH)
        self.assertEqual(res["code"], "auth")
        self.assertEqual(f.log.count("publish"), 2)                                # one retry, never a loop
        self.assertEqual(f.log.count("refresh"), 1)
        self.assertEqual(f.saved[-1], {"notion_reconnect": True})

    def test_a_pasted_secret_is_never_refreshed(self):
        f = Fakes(publishes=[NotionAuthError("401")])
        res = f.send(self.folder, OWN)
        self.assertEqual(res["code"], "auth")
        self.assertEqual(res["sentence"], "Couldn't send to Notion. Notion rejected the secret.")
        self.assertEqual(f.log.count("refresh"), 0)
        self.assertEqual(f.saved, [])                                              # and no "connect again" flag for it

    def test_the_real_refresh_is_not_posted_twice_for_a_token_already_swapped(self):
        """Two sends that both hold the old refresh token make one POST between them."""
        posts = []

        def post(url, json=None, timeout=None):
            posts.append(json)
            return SimpleNamespace(status_code=200, json=lambda: {
                "access_token": "tok-2", "refresh_token": "ref-2", "workspace_name": "W"})
        notion_connect.forget_tokens()
        self.addCleanup(notion_connect.forget_tokens)
        with patch.object(notion_connect.requests, "post", post, create=True):
            for _ in range(2):
                f = Fakes(publishes=[NotionAuthError("401"), URL])
                f.refresh = notion_connect.refresh
                self.assertTrue(notion_sync.send_meeting(
                    self.folder, OAUTH, auto=False, persist=f.persist, publish=f.publish,
                    refresh=f.refresh, update_meta=f.update_meta)["ok"])
        self.assertEqual(len(posts), 1)


# ── the page Gusan made ──────────────────────────────────────────────────────

class TestHub(_Case):
    NEW = {"id": "88888888-8888-8888-8888-888888888888", "title": "Gusan meetings", "url": "https://www.notion.so/x"}

    def test_a_missing_hub_is_made_again_saved_and_the_send_retried(self):
        f = Fakes(publishes=[NotionNotFoundError("gone"), URL], hubs=[self.NEW])
        res = f.send(self.folder, OAUTH)
        self.assertTrue(res["ok"])
        self.assertEqual(f.log[:5], ["publish", "create_hub", "persist", "publish", "update_meta"])
        self.assertEqual(f.saved, [{"notion_hub_page": self.NEW["id"], "notion_parent_page": self.NEW["id"],
                                    "notion_parent_title": "Gusan meetings"}])
        self.assertEqual([c["parent"] for c in f.calls], [HUB, self.NEW["id"]])

    def test_a_page_the_user_picked_is_never_swapped(self):
        cfg = dict(OAUTH, notion_parent_page=PICKED)                   # the hub exists, but notes go elsewhere
        f = Fakes(publishes=[NotionNotFoundError("gone")], hubs=[self.NEW])
        res = f.send(self.folder, cfg)
        self.assertEqual((res["ok"], res["code"]), (False, "no_access"))
        self.assertEqual(res["sentence"], "Couldn't send to Notion. Pick another page in Meeting settings.")
        self.assertEqual((f.log.count("create_hub"), f.saved), (0, []))
        self.assertEqual(self.meta()["notion_error"], "no_access")

    def test_without_a_known_hub_nothing_is_made(self):
        cfg = {k: v for k, v in OAUTH.items() if k != "notion_hub_page"}
        f = Fakes(publishes=[NotionNotFoundError("gone")], hubs=[self.NEW])
        self.assertEqual(f.send(self.folder, cfg)["code"], "no_access")
        self.assertEqual(f.log.count("create_hub"), 0)

    def test_a_hub_that_is_gone_again_is_made_only_once(self):
        f = Fakes(publishes=[NotionNotFoundError("gone"), NotionNotFoundError("gone again")], hubs=[self.NEW])
        res = f.send(self.folder, OAUTH)
        self.assertEqual(res["code"], "no_access")
        self.assertEqual(f.log.count("create_hub"), 1)

    def test_a_hub_that_cannot_be_made_is_the_failure(self):
        f = Fakes(publishes=[NotionNotFoundError("gone")], hubs=[NotionError("net", code="offline")])
        res = f.send(self.folder, OAUTH)
        self.assertEqual((res["ok"], res["code"], f.saved), (False, "offline", []))

    def test_a_refresh_and_a_new_hub_can_both_be_needed(self):
        f = Fakes(publishes=[NotionAuthError("401"), NotionNotFoundError("gone"), URL],
                  refreshes=[tokens()], hubs=[self.NEW])
        res = f.send(self.folder, OAUTH)
        self.assertTrue(res["ok"])
        self.assertEqual(f.log[:7], ["publish", "refresh", "persist", "publish", "create_hub", "persist", "publish"])
        self.assertEqual([c["token"] for c in f.calls], ["tok-1", "tok-2", "tok-2"])


# ── one send at a time ───────────────────────────────────────────────────────

class TestBusy(_Case):
    def test_a_second_send_of_the_same_meeting_is_busy_and_only_one_page_is_made(self):
        inside, release = threading.Event(), threading.Event()
        pages = []

        def slow_publish(token, parent, title, notes, transcript="", meta_line="", *, oauth=False):
            pages.append(title)
            inside.set()
            release.wait(10)
            return URL
        out = {}
        t = threading.Thread(target=lambda: out.update(first=notion_sync.send_meeting(
            self.folder, OAUTH, publish=slow_publish)))
        t.start()
        self.assertTrue(inside.wait(10))
        again = notion_sync.send_meeting(self.folder, OAUTH, auto=True, publish=slow_publish)
        self.assertEqual((again["ok"], again["code"]), (False, "busy"))
        self.assertEqual(again["sentence"], "Already sending this meeting.")
        # a spelling of the same folder is the same folder
        spelled = notion_sync.send_meeting(os.path.join(self.folder, "..", os.path.basename(self.folder)),
                                           OAUTH, publish=slow_publish)
        self.assertEqual(spelled["code"], "busy")
        release.set()
        t.join(10)
        self.assertTrue(out["first"]["ok"])
        self.assertEqual(len(pages), 1)

    def test_another_meeting_is_not_blocked(self):
        other = str(Path(self._tmp.name) / "20261008_120000")
        os.mkdir(other)
        Path(other, "notes.md").write_text("- x", encoding="utf-8")
        inside, release = threading.Event(), threading.Event()

        def slow_publish(*a, **k):
            inside.set()
            release.wait(10)
            return URL
        t = threading.Thread(target=lambda: notion_sync.send_meeting(self.folder, OAUTH, publish=slow_publish))
        t.start()
        self.assertTrue(inside.wait(10))
        self.assertTrue(Fakes().send(other, OAUTH)["ok"])
        release.set()
        t.join(10)

    def test_the_guard_is_let_go_after_a_success_a_failure_and_a_crash(self):
        for outcome in (URL, NotionError("net", code="offline"), RuntimeError("bug")):
            f = Fakes(publishes=[outcome])
            self.assertNotEqual(f.send(self.folder, OAUTH)["code"], "busy")
            f = Fakes(publishes=[URL])
            self.assertTrue(f.send(self.folder, OAUTH)["ok"], outcome)
        self.assertEqual(notion_sync._INFLIGHT, set())

    def test_the_guard_is_let_go_when_even_the_answer_cannot_be_built(self):
        with patch.object(notion_sync, "_send", side_effect=KeyError("x")):
            with self.assertRaises(KeyError):
                notion_sync.send_meeting(self.folder, OAUTH)
        self.assertEqual(notion_sync._INFLIGHT, set())


# ── the line under the title ─────────────────────────────────────────────────

class TestMetaLine(unittest.TestCase):
    def test_when_duration_people_and_the_product(self):
        meta = {"started": "2026-09-29 11:00:00", "duration_sec": 3900, "attendees_list": ["Anna", "David"]}
        self.assertEqual(notion_sync.meta_line(meta), "Tue 29 Sep · 11:00 · 1 h 05 min · Anna, David · Gusan")

    def test_people_fall_back_to_the_older_comma_string(self):
        meta = {"started": "2026-09-29 11:00:00", "attendees": "Anna, David,  anna"}
        self.assertEqual(notion_sync.meta_line(meta), "Tue 29 Sep · 11:00 · Anna, David · Gusan")

    def test_a_bare_meeting_is_just_the_product(self):
        self.assertEqual(notion_sync.meta_line({}), "Gusan")
        self.assertEqual(notion_sync.meta_line(None), "Gusan")

    def test_the_start_comes_from_the_folder_name_when_the_meta_has_none(self):
        # A path for the system the tests run on (a "C:\x\..." string is one name on Linux).
        folder = os.path.join(os.sep, "x", "meetings", "20260929_110000")
        self.assertEqual(notion_sync.meta_line({}, folder), "Tue 29 Sep · 11:00 · Gusan")
        self.assertEqual(notion_sync.meta_line({}, folder + os.sep), "Tue 29 Sep · 11:00 · Gusan")

    def test_junk_in_the_meta_never_raises(self):
        meta = {"started": "yesterday", "duration_sec": "soon", "attendees_list": [3, None]}
        self.assertEqual(notion_sync.meta_line(meta), "yesterday · Gusan")


# ── the real module, as its callers use it ───────────────────────────────────

class TestContract(_Case):
    def test_the_module_exists_and_has_what_its_callers_use(self):
        self.assertIsNotNone(importlib.util.find_spec("notion_sync"))
        used = set()
        for rel in ("main.py", "ui/pages/meeting_views.py", "ui/pages/meetings.py", "ui/notion_sheet.py"):
            used |= set(re.findall(r"\bnotion_sync\.(\w+)", (ROOT / rel).read_text(encoding="utf-8")))
        self.assertIn("send_meeting", used)
        for name in used:
            self.assertTrue(hasattr(notion_sync, name), name)

    def test_the_signature_is_the_one_the_callers_pass(self):
        params = inspect.signature(notion_sync.send_meeting).parameters
        self.assertEqual(list(params)[:2], ["folder", "cfg"])
        for name in ("auto", "persist", "publish", "refresh", "create_hub", "update_meta"):
            self.assertEqual(params[name].kind, inspect.Parameter.KEYWORD_ONLY, name)

    def test_every_answer_has_the_four_keys_and_a_known_code(self):
        answers = []
        for outcome in (URL, NotionAuthError("x"), NotionNotFoundError("x"), NotionError("x", code="rate"),
                        NotionError("x", code="offline"), NotionError("x"), RuntimeError("x")):
            answers.append(Fakes(publishes=[outcome]).send(self.folder, OWN))
        ms.update_meta(self.folder, notion_url="https://www.notion.so/p")
        answers.append(Fakes().send(self.folder, OWN, auto=True))
        for res in answers:
            self.assertEqual(set(res), {"ok", "url", "code", "sentence"})
            self.assertIs(type(res["ok"]), bool)
            self.assertIn(res["code"], notion_sync.CODES)
            self.assertTrue(res["ok"] or res["code"] == "sent" or res["sentence"])

    def test_a_failure_the_ui_gets_says_the_same_as_what_the_callers_expect(self):
        for code in ("auth", "no_access", "rate", "offline", "other"):
            self.assertIn(code, notion_sync.CODES)


class TestAutoSendThroughTheRealModule(_Case):
    """main.AppController._auto_send_notion calls the real notion_sync (the other
    auto-send tests put a fake in sys.modules, which would hide a missing module)."""

    def app(self, **cfg):
        settings = {**OAUTH, "notion_auto_send": True, "notify_when_ready": True, **cfg}
        a = SimpleNamespace(cfg=settings, hints=[], tracked=[], applied=[], history=0, opened=[])
        a.set_settings = lambda d: a.applied.append(dict(d)) or {k: True for k in d}
        a.set_setting = lambda k, v: a.applied.append({k: v}) or True
        a._run_on_gui = lambda fn, *args: fn(*args)
        a.track = lambda event, props=None: a.tracked.append((event, props))
        a.notify_history_changed = lambda: setattr(a, "history", a.history + 1)
        a.show_tray_hint = lambda t, m, on_click=None: a.hints.append((t, m))
        a.show_main = lambda route=None, **o: a.opened.append((route, o))
        a.main_win = SimpleNamespace(isVisible=lambda: True, isMinimized=lambda: False,
                                     current_route=lambda: "home")
        for name in ("_auto_send_notion", "_notion_auto_done"):
            setattr(a, name, getattr(main.AppController, name).__get__(a))
        return a

    def test_the_notes_reach_publish_and_the_meeting_remembers_the_page(self):
        calls = []

        def publish(token, parent, title, notes, transcript="", meta_line="", *, oauth=False):
            calls.append((token, parent, title, notes, transcript, oauth))
            return URL
        a = self.app()
        with patch.object(notion_export, "publish_meeting", publish):
            a._auto_send_notion(self.folder, "Weekly sync", dict(a.cfg))
        self.assertEqual(calls, [("tok-1", HUB, "Weekly sync", "## Summary\n- Ship Friday", "", True)])
        self.assertEqual(self.meta()["notion_url"], URL)
        self.assertIs(self.meta()["notion_auto"], True)
        self.assertEqual(a.tracked, [("meeting_exported", {"target": "notion", "ok": True, "auto": True,
                                                           "mode": "oauth"})])
        self.assertEqual(a.hints, [("Sent to Notion", "Weekly sync")])

    def test_a_meeting_already_in_notion_is_not_sent_again(self):
        ms.update_meta(self.folder, notion_url="https://www.notion.so/earlier")
        a = self.app()
        with patch.object(notion_export, "publish_meeting", side_effect=AssertionError("sent twice")):
            a._auto_send_notion(self.folder, "Weekly sync", dict(a.cfg))
        self.assertEqual((a.tracked, a.hints), ([], []))

    def test_a_refused_token_is_refreshed_saved_through_the_gui_and_the_send_goes_on(self):
        order = []
        pubs = [NotionAuthError("401"), URL]

        def publish(token, *args, **kw):
            order.append(("publish", token))
            out = pubs.pop(0)
            if isinstance(out, Exception):
                raise out
            return out

        def refresh(refresh_token):
            order.append(("refresh", refresh_token))
            return tokens("tok-2", "ref-2")
        a = self.app()
        a.set_settings = lambda d: order.append(("saved", dict(d))) or {k: True for k in d}
        with patch.object(notion_export, "publish_meeting", publish), \
                patch.object(notion_connect, "refresh", refresh):
            a._auto_send_notion(self.folder, "Weekly sync", dict(a.cfg))
        self.assertEqual(order, [("publish", "tok-1"), ("refresh", "ref-1"),
                                 ("saved", {"notion_api_key": "tok-2", "notion_refresh_token": "ref-2"}),
                                 ("publish", "tok-2")])
        self.assertEqual(self.meta()["notion_url"], URL)

    def test_a_connection_notion_refuses_for_good_ends_in_one_hint_and_the_flag(self):
        a = self.app()
        with patch.object(notion_export, "publish_meeting", side_effect=NotionAuthError("401")), \
                patch.object(notion_connect, "refresh", side_effect=ConnectError("expired")):
            a._auto_send_notion(self.folder, "Weekly sync", dict(a.cfg))
        self.assertEqual(self.meta()["notion_error"], "auth")
        self.assertIn({"notion_reconnect": True}, a.applied)
        self.assertEqual(a.hints[0][0], "Couldn't send to Notion")
        self.assertEqual(a.tracked[0][1]["reason"], "auth")


if __name__ == "__main__":
    unittest.main()
