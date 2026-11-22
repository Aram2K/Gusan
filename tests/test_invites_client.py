"""Invite friends, the Qt-free part (invites.py): links and codes, this computer's one-way code, the client of the
server's "invite" function (the network is a fake), every error as one sentence, the once-a-day rules, the cache,
and what the screens show for each state of the program."""
import hashlib
import os
import shutil
import sys
import tempfile
import unittest
from datetime import date, datetime
from types import SimpleNamespace
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import invites  # noqa: E402

LINK = "https://gusan.aibuben.xyz/i/aram-7k2q"


class Resp:
    def __init__(self, data, status=200):
        self._data, self.status_code = data, status

    def json(self):
        if isinstance(self._data, Exception):
            raise self._data
        return self._data


class FakePost:
    """Records every request; answers from a list (or one answer for all)."""

    def __init__(self, *answers):
        self.answers = list(answers)
        self.calls = []

    def __call__(self, url, json=None, headers=None, timeout=None):
        self.calls.append({"url": url, "json": json, "headers": headers, "timeout": timeout})
        a = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(a, Exception):
            raise a
        return a


def auth(token="tok-123", signed_in=True):
    return SimpleNamespace(is_authenticated=signed_in, get_access_token=lambda: token)


def friend(name, status="counted", joined="2026-11-12T10:00:00Z", reason=""):
    return {"name": name, "initial": name[:1], "status": status, "joined_at": joined, "reason": reason}


def me(friends=(), rewards=(), **kw):
    data = {"code": "aram-7k2q", "link": LINK, "first_name": "Aram", "counted": 0, "joined_total": 0,
            "rewards_left_this_year": 12, "friends": list(friends), "rewards": list(rewards), "gift": None,
            "invited_by": None, "can_join": False, "join_until": None}
    data.update(kw)
    return invites.normalize_me(data)


MARIAM = friend("Mariam", joined="2026-11-12T10:00:00Z")
DAVIT = friend("Davit", joined="2026-11-16T10:00:00Z")
NARE = friend("Nare", joined="2026-11-17T10:00:00Z")
DAVIT_C, NARE_C = DAVIT, NARE                    # (every friend who joined counts)


class TestCodes(unittest.TestCase):
    def test_a_full_link_or_a_bare_code(self):
        for text in (LINK, "gusan.aibuben.xyz/i/aram-7k2q", "  https://gusan.aibuben.xyz/i/aram-7k2q/  ",
                     "https://gusan.aibuben.xyz/i/Aram-7K2Q?utm=x", "aram-7k2q", "ARAM-7K2Q", "<aram-7k2q>",
                     "https://gusan.aibuben.xyz/?code=aram-7k2q", "http://aibuben.xyz/invite/aram-7k2q"):
            self.assertEqual(invites.parse_code(text), "aram-7k2q", text)

    def test_anything_else_is_not_a_code(self):
        for text in ("", None, "   ", "hello world", "ab", "https://example.com/", "gusan.aibuben.xyz",
                     "https://gusan.aibuben.xyz/pricing", "a" * 41, "aram 7k2q", "javascript:alert(1)",
                     "-aram", "https://gusan.aibuben.xyz/i/../etc"):
            self.assertEqual(invites.parse_code(text), "", text)

    def test_the_whole_shared_message_pasted(self):
        message = invites.share_text(LINK)
        self.assertEqual(invites.parse_code(message), "aram-7k2q")
        self.assertEqual(invites.parse_code("my code is aram-7k2q, thanks"), "aram-7k2q")
        self.assertEqual(invites.parse_code("see gusan.aibuben.xyz/i/nare-55xk."), "nare-55xk")
        self.assertEqual(invites.parse_code(LINK + "."), "aram-7k2q")
        for text in ("send me an e-mail please", "see example.com/about now", "x" * 700):
            self.assertEqual(invites.parse_code(text), "", text)

    def test_link_for_and_display(self):
        self.assertEqual(invites.link_for("Aram-7K2Q"), LINK)
        self.assertEqual(invites.link_for("not a code"), "")
        self.assertEqual(invites.display_link(LINK + "/"), "gusan.aibuben.xyz/i/aram-7k2q")


class TestDevice(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gusan-invite-")
        self.env = mock.patch.dict(os.environ, {"TRANSCRIBE_APP_DATA_DIR": self.tmp})
        self.env.start()
        invites._device["hash"] = None

    def tearDown(self):
        self.env.stop()
        invites._device["hash"] = None
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_the_hash_is_sha256_of_the_prefixed_id(self):
        h = invites.hash_id("ABC-123")
        self.assertRegex(h, r"^[0-9a-f]{64}$")
        self.assertEqual(h, hashlib.sha256(b"gusan-invite-v1:ABC-123").hexdigest())
        self.assertNotIn("ABC-123", h)

    def test_device_hash_has_the_right_shape_and_is_kept_for_the_run(self):
        with mock.patch.object(invites, "machine_id", return_value="machine-1") as mid:
            first = invites.device_hash()
            again = invites.device_hash()
        self.assertEqual(first, again)
        self.assertRegex(first, r"^[0-9a-f]{64}$")
        self.assertEqual(first, invites.hash_id("machine-1"))
        mid.assert_called_once_with()

    def test_without_a_machine_id_a_random_one_is_kept_in_the_data_folder(self):
        with mock.patch.object(invites, "_windows_machine_guid", side_effect=OSError("no registry")), \
                mock.patch.object(invites, "_mac_platform_uuid", side_effect=OSError("no ioreg")):
            one = invites.machine_id()
            two = invites.machine_id()
        self.assertTrue(one.startswith("fallback:"))
        self.assertEqual(one, two)                                       # kept, not made again
        self.assertRegex(one, r"^fallback:[0-9a-f]{32}$")

    def test_the_mac_reader_parses_ioreg(self):
        out = '  "IOPlatformUUID" = "564D1E2A-1111-2222-3333-ABCDEF012345"\n  "other" = "x"'
        with mock.patch.object(invites.subprocess, "run", return_value=SimpleNamespace(stdout=out)):
            self.assertEqual(invites._mac_platform_uuid(), "564D1E2A-1111-2222-3333-ABCDEF012345")

    def test_the_raw_id_never_leaves_the_computer(self):
        post = FakePost(Resp({"ok": True, "name": "Aram", "gift_ends_at": "2026-12-02T10:00:00Z"}), Resp(me()))
        with mock.patch.object(invites, "machine_id", return_value="RAW-MACHINE-GUID-42"):
            invites.join(auth(), "aram-7k2q", post=post)
            invites.fetch_me(auth(), device=True, post=post)
        self.assertEqual(len(post.calls), 2)
        for c in post.calls:
            self.assertNotIn("RAW-MACHINE-GUID-42", repr(c))
            self.assertEqual(c["json"]["device"], invites.hash_id("RAW-MACHINE-GUID-42"))


class TestCalls(unittest.TestCase):
    def test_every_call_posts_its_action_with_the_token_and_a_short_timeout(self):
        post = FakePost(Resp(me()))
        invites.fetch_me(auth("tok-9"), post=post)
        c = post.calls[0]
        self.assertEqual(c["url"], invites.INVITE_URL)
        self.assertEqual(c["json"], {"action": "me"})                    # no device unless asked
        self.assertEqual(c["headers"]["Authorization"], "Bearer tok-9")
        self.assertEqual(c["timeout"], invites.TIMEOUT)
        self.assertLessEqual(sum(invites.TIMEOUT), 15)

    def test_failures_are_quiet_reasons(self):
        cases = ((Resp({}, 401), "signed_out"), (Resp({}, 403), "signed_out"),
                 (ConnectionError("down"), "network"), (TimeoutError("slow"), "network"),
                 (Resp(ValueError("not json"), 502), "server"), (Resp(["a list"]), "server"),
                 (Resp({"error": "boom"}, 500), "server"))
        for answer, reason in cases:
            with self.assertRaises(invites.InviteError) as e:
                invites.call(auth(), "me", post=FakePost(answer))
            self.assertEqual(e.exception.reason, reason, answer)

    def test_signed_out_never_reaches_the_network(self):
        post = FakePost(Resp(me()))
        self.assertIsNone(invites.fetch_me(auth(signed_in=False), post=post))
        self.assertIsNone(invites.fetch_me(auth(token=None), post=post))
        self.assertIsNone(invites.fetch_me(None, post=post))
        self.assertEqual(invites.join(auth(token=""), "aram-7k2q", post=post), {"ok": False, "reason": "signed_out"})
        self.assertEqual(post.calls, [])

    def test_me_is_normalised(self):
        data = {"code": "Aram-7K2Q", "link": "http://evil.example/x", "counted": "2",
                "friends": [{"name": "<b>Mariam</b>\n", "status": "counted"},
                            {"name": "", "status": "weird"}, {"name": "Lilit", "status": "joined"},
                            "not a friend"],
                "rewards": [{"id": 7, "status": "ready"}, {"id": None, "status": "ready"}, {"id": 8, "status": "x"},
                            {"id": 9, "status": "next_bill_free"}],
                "gift": {"active": 1, "ends_at": "2026-12-02T10:00:00Z"}, "invited_by": {"name": "  Aram  "},
                "can_join": 1}
        m = invites.fetch_me(auth(), post=FakePost(Resp(data)))
        self.assertEqual(m["link"], LINK)                                # never a link the server didn't build
        self.assertEqual(m["code"], "aram-7k2q")
        self.assertEqual(m["friends"][0]["name"], "b Mariam /b")         # plain text, one line
        self.assertEqual(m["friends"][1], {"name": "A friend", "initial": "", "known": False,
                                           "status": "not_counted", "joined_at": None, "reason": ""})
        self.assertEqual(m["friends"][2]["status"], "counted")                 # an older "joined": they joined
        self.assertEqual(len(m["friends"]), 3)
        self.assertEqual(m["rewards"], [{"id": 7, "status": "ready", "starts_at": None, "ends_at": None},
                                        {"id": 9, "status": "credited", "starts_at": None, "ends_at": None}])
        self.assertEqual(m["gift"], {"active": True, "ends_at": "2026-12-02T10:00:00Z"})
        self.assertEqual(m["invited_by"], {"name": "Aram"})
        self.assertFalse(m["can_join"])                                   # joined once: nothing more to enter
        self.assertNotIn("join_until", m)                                 # any time: no deadline any more
        self.assertTrue(invites.normalize_me({"code": "aram-7k2q", "can_join": True})["can_join"])
        self.assertEqual(m["counted"], 2)
        self.assertEqual(m["rewards_left_this_year"], 12)
        self.assertIsNone(invites.normalize_me(None))
        self.assertIsNone(invites.fetch_me(auth(), post=FakePost(ConnectionError())))

    def test_hint(self):
        self.assertEqual(invites.fetch_hint(auth(), post=FakePost(Resp({"found": True, "name": "Aram",
                                                                         "code": "aram-7k2q"}))),
                         {"found": True, "name": "Aram", "code": "aram-7k2q"})
        self.assertFalse(invites.fetch_hint(auth(), post=FakePost(Resp({"found": True, "name": "Aram"})))["found"])
        nameless = invites.fetch_hint(auth(), post=FakePost(Resp({"found": True, "code": "aram-7k2q"})))
        self.assertEqual(nameless, {"found": True, "name": "", "code": "aram-7k2q"})     # "A friend invited you"
        self.assertFalse(invites.fetch_hint(auth(), post=FakePost(Resp({"found": False})))["found"])
        self.assertFalse(invites.fetch_hint(auth(), post=FakePost(ConnectionError()))["found"])

    def test_join(self):
        post = FakePost(Resp({"ok": True, "name": "Aram", "gift_ends_at": "2026-12-02T10:00:00Z"}))
        with mock.patch.object(invites, "device_hash", return_value="d" * 64):
            res = invites.join(auth(), LINK, "hint", post=post)
        self.assertEqual(res, {"ok": True, "name": "Aram", "gift_ends_at": "2026-12-02T10:00:00Z"})
        self.assertEqual(post.calls[0]["json"], {"action": "join", "code": "aram-7k2q", "device": "d" * 64,
                                                 "source": "hint"})
        with mock.patch.object(invites, "device_hash", return_value="d" * 64):
            self.assertEqual(invites.join(auth(), "aram-7k2q", "anything", post=post)["ok"], True)
        self.assertEqual(post.calls[1]["json"]["source"], "paste")
        for reason in ("bad_code", "own_code", "already_joined", "too_late", "not_confirmed", "same_computer",
                       "seen_before", "limit"):
            with mock.patch.object(invites, "device_hash", return_value="d" * 64):
                res = invites.join(auth(), "aram-7k2q", post=FakePost(Resp({"ok": False, "reason": reason}, 400)))
            self.assertEqual(res, {"ok": False, "reason": reason})

    def test_a_bad_code_is_caught_before_the_network(self):
        post = FakePost(Resp({"ok": True}))
        self.assertEqual(invites.join(auth(), "", post=post), {"ok": False, "reason": "empty"})
        self.assertEqual(invites.join(auth(), "not a code!", post=post), {"ok": False, "reason": "bad_code"})
        self.assertEqual(post.calls, [])

    def test_claim(self):
        self.assertFalse(hasattr(invites, "activity"))                    # gone: a friend counts when they join
        c = invites.claim(auth(), 7, post=FakePost(Resp({"ok": True, "status": "next_bill_free", "amount": 1599,
                                                          "currency": "eur"})))
        self.assertEqual(c, {"ok": True, "status": "next_bill_free", "amount": 1599, "currency": "eur"})
        c = invites.claim(auth(), 7, post=FakePost(Resp({"ok": True, "status": "banked"})))
        self.assertEqual(c["status"], "active")                           # no "banked" any more
        c = invites.claim(auth(), 7, post=FakePost(Resp({"ok": True, "status": "active",
                                                          "ends_at": "2026-12-18T10:00:00Z"})))
        self.assertEqual((c["status"], c["ends_at"]), ("active", "2026-12-18T10:00:00Z"))
        self.assertEqual(invites.claim(auth(), 7, post=FakePost(Resp({"ok": False, "reason": "limit"}))),
                         {"ok": False, "reason": "limit"})


class TestSentences(unittest.TestCase):
    REASONS = ("bad_code", "own_code", "already_joined", "not_confirmed", "same_computer",
               "seen_before", "limit", "network", "signed_out", "empty")

    def test_every_join_reason_is_one_plain_sentence(self):
        seen = set()
        for reason in self.REASONS:
            text = invites.join_error(reason)
            self.assertNotEqual(text, invites.JOIN_ERROR_DEFAULT, reason)
            self.assertTrue(text.endswith("."), text)
            self.assertLess(len(text), 90, text)
            self.assertNotIn(reason, text)                                 # never the code word itself
            seen.add(text)
        self.assertEqual(len(seen), len(self.REASONS))                     # each says its own thing
        self.assertEqual(invites.join_error("something new"), invites.JOIN_ERROR_DEFAULT)
        self.assertEqual(invites.join_error(None), invites.JOIN_ERROR_DEFAULT)

    def test_the_server_means_these(self):
        self.assertEqual(invites.join_error("seen_before"),
                         "This email already had a Gusan account, so it can't join an invite.")
        self.assertEqual(invites.join_error("same_computer"), "This computer has already been used with an invite.")
        self.assertEqual(invites.claim_error("not_eligible"),
                         "Rewards are for Free and subscription plans. Your friends still get 2 weeks of Pro.")
        a = SimpleNamespace(is_authenticated=True, get_access_token=lambda: "t")
        res = invites.join(a, "aram-7k2q", post=FakePost(Resp({"ok": True, "name": None,
                                                                "gift_ends_at": "2026-12-02T10:00:00Z"})))
        self.assertEqual((res["ok"], res["name"]), (True, ""))

    def test_claim_reasons(self):
        self.assertEqual(invites.claim_error("whatever"), invites.CLAIM_ERROR_DEFAULT)
        self.assertIn("connection", invites.claim_error("network"))
        for reason in ("already_claimed", "not_found", "limit", "signed_out"):
            self.assertNotEqual(invites.claim_error(reason), invites.CLAIM_ERROR_DEFAULT, reason)


class TestShare(unittest.TestCase):
    def test_codes_are_shown_upper_case_and_typed_in_any_case(self):
        self.assertEqual(invites.display_code("aram-7k2q"), "ARAM-7K2Q")
        self.assertEqual(invites.display_code(LINK), "ARAM-7K2Q")
        self.assertEqual(invites.display_code("not a code!"), "")
        for typed in ("ARAM-7K2Q", "Aram-7k2Q", " aram-7k2q "):
            self.assertEqual(invites.parse_code(typed), "aram-7k2q")

    def test_the_message_and_the_targets(self):
        text = invites.share_text(LINK, "aram-7k2q")
        self.assertIn(LINK, text)
        self.assertIn("ARAM-7K2Q", text)                                  # the code too, to type in Gusan
        self.assertIn("2 weeks of Pro", text)
        self.assertLess(len(text), 170)
        self.assertEqual(invites.parse_code(text), "aram-7k2q")            # a friend can paste the whole message
        self.assertNotIn("code", invites.share_text(LINK))                 # no code known: just the link
        urls = invites.share_urls(LINK, code="aram-7k2q")
        self.assertIn("ARAM-7K2Q", urls["telegram"].split("&text=")[1])
        self.assertEqual(set(urls), {"whatsapp", "telegram", "email", "x", "linkedin"})
        enc = "https%3A%2F%2Fgusan.aibuben.xyz%2Fi%2Faram-7k2q"
        self.assertTrue(urls["whatsapp"].startswith("https://wa.me/?text="))
        self.assertTrue(urls["telegram"].startswith("https://t.me/share/url?url=" + enc + "&text="))
        self.assertNotIn(enc, urls["telegram"].split("&text=")[1])         # the link is not said twice
        self.assertTrue(urls["email"].startswith("mailto:?subject=Try%20Gusan%20with%20me&body="))
        self.assertTrue(urls["x"].startswith("https://x.com/intent/post?text="))
        self.assertEqual(urls["linkedin"], "https://www.linkedin.com/sharing/share-offsite/?url=" + enc)
        for u in urls.values():
            self.assertNotIn(" ", u)
            self.assertIn(enc, u)


class TestOnceADay(unittest.TestCase):
    def test_no_activity_bookkeeping_any_more(self):
        for gone in ("activity", "activity_due", "friend_progress", "DAYS_NEEDED"):
            self.assertFalse(hasattr(invites, gone), gone)

    def test_the_device_check_once_a_day(self):
        self.assertTrue(invites.device_due({}, "2026-11-18"))
        self.assertFalse(invites.device_due({"device_day": "2026-11-18"}, "2026-11-18"))
        self.assertTrue(invites.device_due({"device_day": "2026-11-17"}, "2026-11-18"))

    def test_today_is_the_local_calendar_day(self):
        self.assertEqual(invites.today(datetime(2026, 11, 18, 23, 59)), "2026-11-18")
        self.assertEqual(invites.today(date(2026, 1, 2)), "2026-01-02")
        self.assertRegex(invites.today(), r"^\d{4}-\d{2}-\d{2}$")


class TestAnnouncements(unittest.TestCase):
    def test_the_first_look_announces_nobody(self):
        fresh, known = invites.newly_counted({}, me([MARIAM, DAVIT, friend("Ani", "not_counted")]))
        self.assertEqual(fresh, [])
        self.assertEqual(known, [invites.friend_key(MARIAM), invites.friend_key(DAVIT)])

    def test_a_friend_is_announced_once(self):
        state = {"announced": [invites.friend_key(MARIAM)]}
        fresh, known = invites.newly_counted(state, me([MARIAM, DAVIT_C]))
        self.assertEqual([f["name"] for f in fresh], ["Davit"])
        fresh, _ = invites.newly_counted({"announced": known}, me([MARIAM, DAVIT_C]))
        self.assertEqual(fresh, [])

    def test_the_note(self):
        self.assertEqual(invites.counted_note({"name": "Mariam"}, 1), ("Mariam counts", "2 more friends for your free month"))
        self.assertEqual(invites.counted_note({"name": "Davit"}, 2)[1], "1 more friend for your free month")
        self.assertEqual(invites.counted_note({"name": "Nare"}, 3)[1], "Your free month is ready")


class TestCache(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="gusan-invite-")
        self.env = mock.patch.dict(os.environ, {"TRANSCRIBE_APP_DATA_DIR": self.tmp})
        self.env.start()

    def tearDown(self):
        self.env.stop()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_each_account_has_its_own_state(self):
        self.assertEqual(invites.load_state("user-a"), {})
        invites.update_state("user-a", me=me([MARIAM]), activity_day="2026-11-18")
        invites.update_state("user-b", join_dismissed=True)
        a = invites.load_state("user-a")
        self.assertEqual(a["activity_day"], "2026-11-18")
        self.assertEqual(a["me"]["friends"][0]["name"], "Mariam")
        self.assertEqual(invites.load_state("user-b"), {"join_dismissed": True})
        invites.update_state("user-a", activity_day="2026-11-19")
        self.assertEqual(invites.load_state("user-a")["activity_day"], "2026-11-19")
        self.assertEqual(invites.load_state("user-a")["me"]["friends"][0]["name"], "Mariam")   # merged, kept
        self.assertTrue(os.path.isfile(os.path.join(self.tmp, invites.CACHE_FILE)))

    def test_no_account_no_file(self):
        self.assertEqual(invites.load_state(None), {})
        self.assertEqual(invites.update_state(None, x=1), {"x": 1})
        self.assertFalse(os.path.isfile(os.path.join(self.tmp, invites.CACHE_FILE)))

    def test_a_broken_file_is_an_empty_state(self):
        with open(os.path.join(self.tmp, invites.CACHE_FILE), "w", encoding="utf-8") as f:
            f.write("{not json")
        self.assertEqual(invites.load_state("user-a"), {})
        invites.update_state("user-a", activity_day="2026-11-18")
        self.assertEqual(invites.load_state("user-a"), {"activity_day": "2026-11-18"})


class TestView(unittest.TestCase):
    def kinds(self, v):
        return [s["kind"] for s in v["slots"]]

    def test_nothing_yet(self):
        v = invites.view(me(), "")
        self.assertEqual(self.kinds(v), ["empty"] * 3)
        self.assertEqual((v["ticket"], v["to_go"], v["dots"], v["ready"]), ("locked", "3 friends to go", (0, 0), False))
        self.assertEqual(v["lead"], "Share your link or code. When 3 friends join, your month is ready.")
        self.assertEqual(v["friends"], [])
        self.assertEqual(invites.view(None)["ticket"], "locked")

    def test_a_friend_counts_when_they_join(self):
        v = invites.view(me([MARIAM, DAVIT]), "")
        self.assertEqual(self.kinds(v), ["done", "done", "empty"])
        self.assertEqual([s["sub"] for s in v["slots"]], ["Counts", "Counts", "Not yet"])
        self.assertEqual(v["lead"], "Mariam and Davit count. One more friend to go.")
        self.assertEqual((v["ticket"], v["to_go"], v["dots"]), ("locked", "1 more friend to go", (2, 0)))
        rows = {r["name"]: r for r in v["friends"]}
        self.assertEqual((rows["Mariam"]["chip"], rows["Mariam"]["tone"], rows["Mariam"]["meta"]),
                         ("Counts", "ok", "Joined 12 Nov"))
        self.assertEqual(invites.view(me([MARIAM]), "")["lead"], "Mariam counts. Two more friends to go.")

    def test_a_friend_whose_name_the_server_keeps(self):
        m = me([{"name": "A friend", "initial": "", "status": "counted", "joined_at": "2026-11-12T10:00:00Z"},
                {"name": None, "status": "counted", "joined_at": "2026-11-13T10:00:00Z"}],
               invited_by={"name": None})
        self.assertEqual([(f["name"], f["initial"], f["known"]) for f in m["friends"]],
                         [("A friend", "", False)] * 2)                    # no made-up letter
        self.assertEqual(m["invited_by"], {"name": ""})
        v = invites.view(m, "")
        self.assertEqual(v["lead"], "Two friends count. One more friend to go.")
        self.assertEqual([s["initial"] for s in v["slots"]], ["", "", ""])
        self.assertEqual(invites._names(["Mariam", "A friend", "A friend"]), "Mariam and 2 friends")
        self.assertEqual(invites._names(["A friend"]), "A friend")
        self.assertEqual(invites._names(["Mariam", "A friend"]), "Mariam and a friend")
        self.assertEqual(invites.counted_note(m["friends"][0], 1)[0], "A friend counts")
        self.assertEqual(me([MARIAM])["friends"][0]["initial"], "M")

    def test_friends_who_do_not_count_are_said_plainly(self):
        v = invites.view(me([friend("Ani", "not_counted", reason="same_computer"), NARE,
                             friend("Gor", "not_counted", reason="seen_before")]), "")
        rows = {r["name"]: r for r in v["friends"]}
        self.assertEqual((rows["Ani"]["chip"], rows["Ani"]["meta"]),
                         ("Doesn't count", "Their computer was already used with an invite"))
        self.assertEqual(rows["Gor"]["meta"], "Their email had a Gusan account before")
        self.assertEqual(rows["Nare"]["chip"], "Counts")
        self.assertEqual(self.kinds(v), ["done", "empty", "empty"])          # Ani and Gor take no slot
        self.assertEqual(v["slots"][0]["name"], "Nare")

    def test_ready(self):
        v = invites.view(me([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "ready"}]), "free")
        self.assertEqual(self.kinds(v), ["done"] * 3)
        self.assertEqual((v["ticket"], v["ready_id"], v["ready"]), ("ready", "r1", True))
        self.assertEqual(v["lead"], "3 friends joined. Your month is ready.")

    def test_claimed_keeps_the_friends_who_earned_it_until_a_new_one_comes(self):
        active = {"id": "r1", "status": "active", "ends_at": "2026-12-18T10:00:00Z"}
        v = invites.view(me([MARIAM, DAVIT, NARE], [active]), "")
        self.assertEqual(self.kinds(v), ["done"] * 3)
        self.assertEqual((v["ticket"], v["until"], v["dots"]), ("active", "18 Dec", (0, 0)))
        self.assertEqual(v["lead"], "Your free month is on. 3 more friends make another one.")
        newcomer = friend("Lilit", joined="2026-11-20T10:00:00Z")
        v = invites.view(me([MARIAM, DAVIT, NARE, newcomer], [active]), "")
        self.assertEqual(self.kinds(v), ["done", "empty", "empty"])         # the next three
        self.assertEqual(v["slots"][0]["name"], "Lilit")
        self.assertEqual((v["ticket"], v["dots"]), ("locked", (1, 0)))

    def test_a_subscribers_next_bill_is_free(self):
        for word in ("credited", "next_bill_free"):
            v = invites.view(me([MARIAM, DAVIT, NARE], [{"id": "r1", "status": word}]), "monthly")
            self.assertEqual(v["ticket"], "credited", word)
            self.assertEqual(v["lead"], "Your next bill is free. 3 more friends make another one.")
            self.assertEqual(self.kinds(v), ["done"] * 3)

    def test_the_second_month(self):
        four = [MARIAM, DAVIT, NARE, friend("Lilit", joined="2026-11-21T10:00:00Z")]
        v = invites.view(me(four, [{"id": "r1", "status": "used"}]), "")
        self.assertEqual(self.kinds(v), ["done", "empty", "empty"])
        self.assertEqual(v["slots"][0]["name"], "Lilit")
        self.assertEqual((v["ticket"], v["to_go"]), ("locked", "2 more friends to go"))

    def test_lifetime_and_admin_earn_no_months(self):
        for plan in ("lifetime", "admin", "Lifetime"):
            v = invites.view(me([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "ready"}]), plan)
            self.assertEqual((v["ticket"], v["ready"], v["eligible"]), ("ineligible", False, False), plan)
        self.assertTrue(invites.eligible("monthly") and invites.eligible("") and invites.eligible(None))

    def test_twelve_months_a_year(self):
        v = invites.view(me([MARIAM], rewards_left_this_year=0), "")
        self.assertTrue(v["limit_reached"])
        self.assertIn("12 free months", v["lead"])

    def test_each_friend_keeps_one_colour_everywhere(self):
        v = invites.view(me([MARIAM, DAVIT, NARE]), "")
        by_slot = {s["name"]: s["idx"] for s in v["slots"]}
        by_row = {r["name"]: r["idx"] for r in v["friends"]}
        self.assertEqual(by_slot, by_row)
        self.assertEqual(sorted(by_row.values()), [0, 1, 2])


class TestServerReasons(unittest.TestCase):
    """The reasons and statuses the server added on 2026-11-20."""

    def test_new_join_reasons_in_plain_words(self):
        self.assertEqual(invites.join_error("same_network"),
                         "Too many friends joined from this internet connection lately. Try again from another "
                         "network later.")
        self.assertEqual(invites.join_error("disposable_email"),
                         "Temporary email addresses can't join an invite. Use your regular email.")
        self.assertEqual(invites.join_error("limit"), "This invite can't take more friends right now. Try again later.")
        res = invites.join(auth(), "aram-7k2q", post=FakePost(Resp({"ok": False, "reason": "disposable_email"})))
        self.assertEqual(res, {"ok": False, "reason": "disposable_email"})

    def test_a_month_being_applied_is_ready_but_busy(self):
        m = me([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "crediting"}])
        self.assertEqual(m["rewards"][0]["status"], "crediting")
        v = invites.view(m, "monthly")
        self.assertEqual(v["ticket"], "crediting")
        self.assertFalse(v["ready"])                                       # nothing to claim: no READY pill
        self.assertIsNone(v["ready_id"])
        self.assertEqual([s["kind"] for s in v["slots"]], ["done"] * 3)
        self.assertEqual(v["dots"], (3, 0))
        self.assertEqual(v["lead"], "3 friends joined. Applying your free month…")
        v = invites.view(me([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "credited"}]), "monthly")
        self.assertEqual(v["ticket"], "credited")                          # what it becomes

    def test_a_claim_that_errors_on_the_server_is_calm(self):
        res = invites.claim(auth(), "r1", post=FakePost(Resp({"error": "server_error"}, 500)))
        self.assertEqual(res, {"ok": False, "reason": "server"})
        text = invites.claim_error("server")
        self.assertNotEqual(text, invites.CLAIM_ERROR_DEFAULT)
        self.assertIn("Check again in a minute", text)
        self.assertNotIn("error", text.lower())


class TestPlanLine(unittest.TestCase):
    def test_pro_from_invites_reads_until_a_date(self):
        import account_actions
        a = SimpleNamespace(is_authenticated=True, is_pro=True, plan="invite", period_end="2026-12-18T10:00:00Z",
                            cancel_at_period_end=False, user_email="a@b.c", user_name="Aram", is_admin=False,
                            commitment_ends_at=None, cancels_at=None)
        v = account_actions.account_view(a, {})
        self.assertEqual(v["plan_line"], "Pro · until 18 Dec 2026")
        self.assertEqual(v["tier"], "pro")
        self.assertFalse(v["buttons"]["manage"])                              # nothing to manage: it just ends
        import entitlements
        self.assertTrue(entitlements.is_invite_plan(a))
        self.assertFalse(entitlements.is_invite_plan(SimpleNamespace(is_pro=True, plan="monthly")))

    def test_the_sidebar_pill_counts_the_days_left(self):
        import account_actions
        from datetime import timezone
        now = datetime(2026, 11, 18, 9, 0, tzinfo=timezone.utc)
        a = SimpleNamespace(is_pro=True, plan="invite", period_end="2026-12-02T10:00:00Z")
        self.assertEqual(account_actions.invite_badge(a, now), "Pro · 15 days")
        a.period_end = "2026-11-18T20:00:00Z"
        self.assertEqual(account_actions.invite_badge(a, now), "Pro · 1 day")
        a.period_end = "2026-11-01T10:00:00Z"
        self.assertEqual(account_actions.invite_badge(a, now), "")                # over: the plain pill
        self.assertEqual(account_actions.invite_badge(SimpleNamespace(is_pro=True, plan="monthly",
                                                                      period_end="2026-12-02T10:00:00Z"), now), "")


if __name__ == "__main__":
    unittest.main()
