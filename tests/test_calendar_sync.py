import http.client
import threading
import unittest
from unittest.mock import MagicMock, patch

import calendar_sync


def _resp(status, body):
    r = MagicMock()
    r.status_code = status
    r.json.return_value = body
    return r


class TestCalendarRefresh(unittest.TestCase):
    CFG = {"google_oauth_client_id": "cid", "google_oauth_client_secret": "sec"}

    def setUp(self):
        with calendar_sync._token_lock:
            calendar_sync._token.update(value="", expires=0.0)

    def test_only_invalid_grant_forgets_the_calendar(self):
        with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync.storage, "write_secret") as write, \
             patch.object(calendar_sync.requests, "post",
                          return_value=_resp(400, {"error": "invalid_grant"})):
            with self.assertRaises(calendar_sync.CalendarAuthError):
                calendar_sync._access_token(self.CFG)
        write.assert_called_once_with(calendar_sync.REFRESH_SECRET, "")

    def test_a_misconfigured_client_keeps_the_users_grant(self):
        for body in ({"error": "invalid_client"}, {"error": "invalid_request"}):
            with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
                 patch.object(calendar_sync.storage, "write_secret") as write, \
                 patch.object(calendar_sync.requests, "post", return_value=_resp(401, body)):
                with self.assertRaises(calendar_sync.CalendarError) as ctx:
                    calendar_sync._access_token(self.CFG)
            self.assertNotIsInstance(ctx.exception, calendar_sync.CalendarAuthError)
            write.assert_not_called()

    def test_no_client_id_never_calls_google(self):
        with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync, "GOOGLE_CLIENT_ID", ""), \
             patch.object(calendar_sync.requests, "post") as post:
            with self.assertRaises(calendar_sync.CalendarError):
                calendar_sync._access_token({})
        post.assert_not_called()


class TestCalendarConnect(unittest.TestCase):
    CFG = {"google_oauth_client_id": "cid"}

    def _run_connect(self, hits, token_response=None, write_ok=True, timeout=5):
        """Run connect() and replay ``hits`` (query strings; "{state}" is
        replaced with the real state) against its loopback listener."""
        result = {}

        def fake_browser(url):
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            port = int(q["redirect_uri"][0].rsplit(":", 1)[1])
            state = q["state"][0]

            def hit_all():
                for h in hits:
                    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                    conn.request("GET", "/?" + h.replace("{state}", state))
                    result.setdefault("codes", []).append(conn.getresponse().status)
                    conn.close()
            t = threading.Thread(target=hit_all, daemon=True)
            result["thread"] = t
            t.start()
            return True

        tok = token_response or _resp(200, {"access_token": "at", "refresh_token": "rt",
                                            "scope": "openid calendar.events.readonly",
                                            "expires_in": 3600})
        with patch.object(calendar_sync.requests, "post", return_value=tok) as post, \
             patch.object(calendar_sync.storage, "write_secret", return_value=write_ok):
            try:
                calendar_sync.connect(self.CFG, open_browser=fake_browser, timeout=timeout)
                result["ok"] = True
            except calendar_sync.CalendarError as e:
                result["error"] = str(e)
            result["post"] = post
        if "thread" in result:
            result["thread"].join(5)   # the last response may still be in flight
        return result

    def test_a_forged_error_without_our_state_does_not_end_the_sign_in(self):
        res = self._run_connect(["error=access_denied", "code=abc&state={state}"])
        self.assertTrue(res.get("ok"), res)
        self.assertEqual(res["codes"], [400, 200])

    def test_an_unreadable_token_answer_is_a_plain_error(self):
        bad = _resp(200, None)
        bad.json.side_effect = ValueError("not json")
        res = self._run_connect(["code=abc&state={state}"], token_response=bad)
        self.assertIn("couldn't be read", res.get("error", ""))

    def test_connect_fails_when_the_token_cannot_be_kept(self):
        res = self._run_connect(["code=abc&state={state}"], write_ok=False)
        self.assertIn("keyring", res.get("error", ""))
        revoked = [c for c in res["post"].call_args_list if c.args[0] == calendar_sync.REVOKE_URL]
        self.assertEqual(len(revoked), 1)

    def test_an_idle_connection_cannot_pin_the_sign_in(self):
        import socket
        import time

        def idle_browser(url):
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            port = int(q["redirect_uri"][0].rsplit(":", 1)[1])
            s = socket.create_connection(("127.0.0.1", port))
            self.addCleanup(s.close)   # connects, never sends
            return True

        with patch.object(calendar_sync._Callback, "timeout", 0.5):
            start = time.monotonic()
            with self.assertRaises(calendar_sync.CalendarError):
                calendar_sync.connect(self.CFG, open_browser=idle_browser, timeout=1)
            self.assertLess(time.monotonic() - start, 4)


class TestConnectCancel(unittest.TestCase):
    CFG = {"google_oauth_client_id": "cid"}

    def test_no_browser_fails_at_once(self):
        import time
        start = time.monotonic()
        with self.assertRaises(calendar_sync.CalendarError) as ctx:
            calendar_sync.connect(self.CFG, open_browser=lambda url: False, timeout=30)
        self.assertIn("browser", str(ctx.exception))
        self.assertLess(time.monotonic() - start, 2)

    def test_a_cancel_set_before_start_opens_no_browser(self):
        cancel = threading.Event()
        cancel.set()
        browser = MagicMock()
        with self.assertRaises(calendar_sync.CalendarCancelled):
            calendar_sync.connect(self.CFG, open_browser=browser, cancel=cancel)
        browser.assert_not_called()

    def test_cancel_stops_the_waiting_listener_quickly(self):
        import time
        cancel = threading.Event()

        def browser(url):
            threading.Timer(0.3, cancel.set).start()
            return True

        start = time.monotonic()
        with self.assertRaises(calendar_sync.CalendarCancelled):
            calendar_sync.connect(self.CFG, open_browser=browser, timeout=30, cancel=cancel)
        self.assertLess(time.monotonic() - start, 5)

    def test_cancel_during_the_code_exchange_keeps_nothing(self):
        cancel = threading.Event()

        def browser(url):
            import urllib.parse
            q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
            port = int(q["redirect_uri"][0].rsplit(":", 1)[1])

            def hit():
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                conn.request("GET", "/?code=abc&state=" + q["state"][0])
                conn.getresponse()
                conn.close()
            threading.Thread(target=hit, daemon=True).start()
            return True

        def post(url, **kw):
            if url == calendar_sync.TOKEN_URL:
                cancel.set()                    # the user clicks Cancel right now
                return _resp(200, {"access_token": "at", "refresh_token": "rt",
                                   "scope": "calendar.events.readonly"})
            return _resp(200, {})

        with patch.object(calendar_sync.requests, "post", side_effect=post) as p, \
             patch.object(calendar_sync.storage, "write_secret", return_value=True) as write:
            with self.assertRaises(calendar_sync.CalendarCancelled):
                calendar_sync.connect(self.CFG, open_browser=browser, timeout=10, cancel=cancel)
        write.assert_not_called()
        self.assertIn(calendar_sync.REVOKE_URL, [c.args[0] for c in p.call_args_list])


class TestDisconnect(unittest.TestCase):
    def test_forget_local_wipes_the_token_without_network(self):
        with calendar_sync._token_lock:
            calendar_sync._token.update(value="at", expires=9e18)
        with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync.storage, "write_secret") as write, \
             patch.object(calendar_sync.requests, "post") as post:
            self.assertEqual(calendar_sync.forget_local(), "rt")
        write.assert_called_once_with(calendar_sync.REFRESH_SECRET, "")
        post.assert_not_called()
        self.assertEqual(calendar_sync._token["value"], "")

    def test_disconnect_revokes_the_kept_token(self):
        with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync.storage, "write_secret"), \
             patch.object(calendar_sync.requests, "post") as post:
            calendar_sync.disconnect()
        post.assert_called_once()
        self.assertEqual(post.call_args.args[0], calendar_sync.REVOKE_URL)
        self.assertEqual(post.call_args.kwargs["params"], {"token": "rt"})

    def test_revoke_without_a_token_is_a_no_op(self):
        with patch.object(calendar_sync.requests, "post") as post:
            calendar_sync.revoke("")
        post.assert_not_called()


class TestLoopbackServer(unittest.TestCase):
    def test_no_other_socket_can_take_the_listening_port(self):
        import socket
        server = calendar_sync._LoopbackServer(("127.0.0.1", 0), calendar_sync._Callback)
        self.addCleanup(server.server_close)
        port = server.server_address[1]
        other = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self.addCleanup(other.close)
        other.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        with self.assertRaises(OSError):
            other.bind(("127.0.0.1", port))


class TestBrowserPages(unittest.TestCase):
    def test_pages_say_gusan(self):
        for page in (calendar_sync._OK_HTML, calendar_sync._FAIL_HTML):
            self.assertIn("Gusan", page)
            self.assertNotIn("Transcribe", page)
        self.assertIn("Calendar connected to Gusan", calendar_sync._OK_HTML)


# ── pure parsing and timing ──────────────────────────────────────────────────
from datetime import datetime, timedelta, timezone  # noqa: E402

T = datetime(2026, 10, 7, 14, 30, tzinfo=timezone.utc)


def _event(**kw):
    ev = {"id": "ev1", "summary": "Weekly product sync", "status": "confirmed",
          "start": {"dateTime": "2026-10-07T14:30:00Z"},
          "end": {"dateTime": "2026-10-07T15:30:00+00:00"},
          "attendees": [
              {"email": "aram@aibuben.xyz", "self": True, "responseStatus": "accepted"},
              {"email": "anna.petrosyan@example.com", "displayName": "Anna Petrosyan"},
              {"email": "david.lee@example.com"},
              {"email": "mariam@example.com", "displayName": "Mariam"},
              {"email": "sam@example.com", "displayName": "Sam Hill"},
              {"email": "room-4b@resource.calendar.google.com", "resource": True},
          ]}
    ev.update(kw)
    return ev


def _meeting(mid="m1", start_min=0, length_min=60, link="https://meet.google.com/abc-defg-hij",
             title="Sync"):
    st = T + timedelta(minutes=start_min)
    return {"id": mid, "title": title, "start": st, "end": st + timedelta(minutes=length_min),
            "attendees": [], "others": [], "link": link}


class TestParseEvent(unittest.TestCase):
    def test_google_meet_event(self):
        m = calendar_sync.parse_event(_event(hangoutLink="https://meet.google.com/abc-defg-hij",
                                             recurringEventId="series1"))
        self.assertEqual(m["link"], "https://meet.google.com/abc-defg-hij")
        self.assertEqual(m["call_type"], "Google Meet")
        self.assertEqual(m["recurring_id"], "series1")
        self.assertEqual(m["start"], T)
        self.assertEqual(m["end"], T + timedelta(hours=1))
        self.assertEqual(m["others"], ["Anna Petrosyan", "David Lee", "Mariam", "Sam Hill"])
        self.assertIn("Aram", m["attendees"])            # you, for notes attribution
        self.assertEqual(m["people"], "Anna, David +2")

    def test_zoom_link_in_the_location(self):
        m = calendar_sync.parse_event(_event(location="https://us02web.zoom.us/j/8812345678?pwd=Ab.c1"))
        self.assertEqual(m["link"], "https://us02web.zoom.us/j/8812345678?pwd=Ab.c1")
        self.assertEqual(m["call_type"], "Zoom")
        self.assertEqual(m["recurring_id"], "")

    def test_teams_link_in_an_html_description(self):
        desc = ('<p>Microsoft Teams meeting</p><a href="https://teams.microsoft.com/l/meetup-join/'
                '19%3ameeting_N2Q%40thread.v2/0?context=%7b%22Tid%22%7d&amp;x=1">Join the meeting now</a>'
                ' <a href="https://aka.ms/JoinTeamsMeeting">Help</a>')
        m = calendar_sync.parse_event(_event(description=desc))
        self.assertTrue(m["link"].startswith("https://teams.microsoft.com/l/meetup-join/19%3ameeting"))
        self.assertTrue(m["link"].endswith("%7d&x=1"))
        self.assertEqual(m["call_type"], "Teams")

    def test_teams_free_and_webex_links(self):
        m = calendar_sync.parse_event(_event(description="Join <https://teams.live.com/meet/9387?p=xy>"))
        self.assertEqual((m["link"], m["call_type"]), ("https://teams.live.com/meet/9387?p=xy", "Teams"))
        desc = "Help: https://help.webex.com/en-us\nJoin: https://acme.webex.com/acme/j.php?MTID=m12."
        m = calendar_sync.parse_event(_event(description=desc))
        self.assertEqual((m["link"], m["call_type"]), ("https://acme.webex.com/acme/j.php?MTID=m12", "Webex"))

    def test_location_wins_over_description_and_google_link_wins_over_both(self):
        m = calendar_sync.parse_event(_event(location="zoom.us/j/1", description="https://meet.google.com/x-y-z"))
        self.assertEqual(m["link"], "https://zoom.us/j/1")
        m = calendar_sync.parse_event(_event(hangoutLink="https://meet.google.com/a-b-c", location="zoom.us/j/1"))
        self.assertEqual(m["call_type"], "Google Meet")

    def test_conference_data_video_entry_and_unknown_service(self):
        conf = {"conferenceSolution": {"name": "Zoom Meeting"},
                "entryPoints": [{"entryPointType": "phone", "uri": "tel:+1"},
                                {"entryPointType": "video", "uri": "https://zoom.us/j/42"}]}
        m = calendar_sync.parse_event(_event(conferenceData=conf))
        self.assertEqual((m["link"], m["call_type"]), ("https://zoom.us/j/42", "Zoom"))
        conf = {"conferenceSolution": {"name": "Jitsi"},
                "entryPoints": [{"entryPointType": "video", "uri": "https://meet.jit.si/room"}]}
        m = calendar_sync.parse_event(_event(conferenceData=conf))
        self.assertEqual((m["link"], m["call_type"]), ("https://meet.jit.si/room", "other"))

    def test_no_link_and_lookalike_links(self):
        for loc in ("Room 4B", "https://evil.example/meet.google.com/abc", "https://notzoom.us/j/1",
                    "https://zoom.us.evil.example/j/1", "https://support.zoom.us/hc/en-us",
                    "https://teams.microsoft.com/meetingOptions/?x=1"):
            m = calendar_sync.parse_event(_event(location=loc))
            self.assertEqual((m["link"], m["call_type"]), ("", ""), loc)
        self.assertEqual(m["location"], "https://teams.microsoft.com/meetingOptions/?x=1")

    def test_events_that_are_not_meetings_to_record(self):
        declined = _event()
        declined["attendees"][0]["responseStatus"] = "declined"
        for ev in (_event(status="cancelled"), _event(eventType="focusTime"),
                   _event(eventType="outOfOffice"), _event(eventType="workingLocation"),
                   _event(start={"date": "2026-10-07"}, end={"date": "2026-10-08"}),
                   _event(start={"dateTime": "not a date"}),
                   _event(start={"dateTime": "2026-10-07T14:30:00"}),     # no offset
                   declined, "junk", None):
            self.assertIsNone(calendar_sync.parse_event(ev), ev)

    def test_untitled_and_no_attendees(self):
        m = calendar_sync.parse_event(_event(summary="   ", attendees=None))
        self.assertEqual(m["title"], "Untitled meeting")
        self.assertEqual((m["others"], m["people"]), ([], ""))


class TestPeopleLabel(unittest.TestCase):
    def test_labels(self):
        label = calendar_sync.people_label
        self.assertEqual(label([]), "")
        self.assertEqual(label(None), "")
        self.assertEqual(label(["Anna Petrosyan"]), "Anna")
        self.assertEqual(label(["Anna Petrosyan", "David Lee"]), "Anna, David")
        self.assertEqual(label(["Anna", "David", "Mariam", "Sam"]), "Anna, David +2")
        self.assertEqual(label(["Anna", "David", "Mariam"], max_names=1), "Anna +2")

    def test_full_names_when_first_names_clash_or_are_titles(self):
        label = calendar_sync.people_label
        self.assertEqual(label(["David Lee", "David Kim", "Anna"]), "David Lee, David Kim +1")
        self.assertEqual(label(["Dr. Smith", "Anna"]), "Dr. Smith, Anna")

    def test_attendee_dicts_and_duplicates(self):
        label = calendar_sync.people_label
        self.assertEqual(label([{"email": "anna.p@x.com"}, {"displayName": "David"},
                                {"email": "room@x", "resource": True}, "David", " "]), "Anna, David")


class TestShouldPrompt(unittest.TestCase):
    def test_window_edges(self):
        m = _meeting(length_min=60)
        sp = calendar_sync.should_prompt
        self.assertFalse(sp(m, T - timedelta(seconds=61)))
        self.assertTrue(sp(m, T - timedelta(seconds=60)))
        self.assertTrue(sp(m, T))
        self.assertTrue(sp(m, T + timedelta(seconds=300)))
        self.assertFalse(sp(m, T + timedelta(seconds=301)))

    def test_a_short_meeting_stops_at_its_end(self):
        m = _meeting(length_min=2)
        self.assertTrue(calendar_sync.should_prompt(m, T + timedelta(seconds=120)))
        self.assertFalse(calendar_sync.should_prompt(m, T + timedelta(seconds=121)))


class TestReminderDue(unittest.TestCase):
    def test_due_and_filters(self):
        due = calendar_sync.reminder_due
        a = _meeting("a")
        self.assertIs(due([a], T - timedelta(seconds=60)), a)
        self.assertIsNone(due([a], T - timedelta(seconds=61)))
        self.assertIsNone(due([a], T, muted={"a"}))
        self.assertIsNone(due([a], T, prompted={"a": "2026-10-07T15:30:00+00:00"}))
        self.assertIsNone(due([], T))
        self.assertIsNone(due(None, T))
        nolink = _meeting("n", link="")
        self.assertIs(due([nolink], T), nolink)
        self.assertIsNone(due([nolink], T, only_with_link=True))
        self.assertIsNone(due(["junk", {"id": "x"}, None], T))

    def test_overlapping_meetings_closest_start_first(self):
        early = _meeting("early", start_min=-4)       # started 4 min ago
        soon = _meeting("soon", start_min=1)          # starts in 1 min
        self.assertEqual(calendar_sync.reminder_due([early, soon], T + timedelta(seconds=30))["id"], "soon")
        self.assertEqual(calendar_sync.reminder_due([early, soon], T + timedelta(seconds=30),
                                                    prompted={"soon"})["id"], "early")


class TestVisibleUpcoming(unittest.TestCase):
    def test_drops_ended_sorts_and_dedupes(self):
        ended = _meeting("ended", start_min=-60, length_min=30)
        running = _meeting("running", start_min=-10, length_min=30)
        later = _meeting("later", start_min=90)
        soon = _meeting("soon", start_min=1, link="")
        rows = calendar_sync.visible_upcoming([later, ended, soon, running, dict(later), "junk"], T)
        self.assertEqual([m["id"] for m in rows], ["running", "soon", "later"])
        rows = calendar_sync.visible_upcoming([later, soon, running], T, only_with_link=True)
        self.assertEqual([m["id"] for m in rows], ["running", "later"])
        self.assertEqual(calendar_sync.visible_upcoming([ended], T), [])

    def test_across_midnight(self):
        late = datetime(2026, 10, 7, 21, 0, tzinfo=timezone.utc)
        tonight = {"id": "t", "title": "Late call", "start": late + timedelta(hours=2, minutes=30),
                   "end": late + timedelta(hours=3), "link": ""}
        tomorrow = {"id": "m", "title": "Early call", "start": late + timedelta(hours=3, minutes=30),
                    "end": late + timedelta(hours=4), "link": ""}
        rows = calendar_sync.visible_upcoming([tomorrow, tonight], late)
        self.assertEqual([m["id"] for m in rows], ["t", "m"])
        utc = timezone.utc
        self.assertEqual(calendar_sync.when_parts(tonight, late, tz=utc),
                         {"time": "23:30", "rel": "", "day": ""})
        self.assertEqual(calendar_sync.when_parts(tomorrow, late, tz=utc),
                         {"time": "00:30", "rel": "", "day": "Tomorrow"})
        new_york = timezone(timedelta(hours=-5))        # 00:30 UTC tomorrow is 19:30 today there
        self.assertEqual(calendar_sync.when_parts(tomorrow, late, tz=new_york),
                         {"time": "19:30", "rel": "", "day": ""})


class TestLabels(unittest.TestCase):
    def test_when_parts_rel(self):
        m = _meeting(length_min=30)
        wp = calendar_sync.when_parts
        utc = timezone.utc
        self.assertEqual(wp(m, T - timedelta(seconds=30), tz=utc), {"time": "14:30", "rel": "in 1 min", "day": ""})
        self.assertEqual(wp(m, T - timedelta(minutes=25), tz=utc)["rel"], "in 25 min")
        self.assertEqual(wp(m, T - timedelta(minutes=61), tz=utc)["rel"], "")
        self.assertEqual(wp(m, T, tz=utc)["rel"], "Now")
        self.assertEqual(wp(m, T + timedelta(minutes=30), tz=utc)["rel"], "")

    def test_reminder_text(self):
        m = _meeting()
        rt = calendar_sync.reminder_text
        self.assertEqual(rt(m, T - timedelta(seconds=60)), "Starts in 1 min")
        self.assertEqual(rt(m, T - timedelta(seconds=5)), "Starts in 1 min")
        self.assertEqual(rt(m, T), "Starting now")
        self.assertEqual(rt(m, T + timedelta(seconds=59)), "Starting now")
        self.assertEqual(rt(m, T + timedelta(minutes=2, seconds=10)), "Started 2 min ago")


class TestUpcoming(unittest.TestCase):
    CFG = {"google_oauth_client_id": "cid"}

    def setUp(self):
        with calendar_sync._token_lock:
            calendar_sync._token.update(value="", expires=0.0)

    def test_window_paging_and_parsing(self):
        page1 = _resp(200, {"items": [_event(id="a"), _event(id="x", status="cancelled")],
                            "nextPageToken": "p2"})
        page2 = _resp(200, {"items": [_event(id="b", location="zoom.us/j/7")]})
        with patch.object(calendar_sync, "_access_token", return_value="at"), \
             patch.object(calendar_sync.requests, "get", side_effect=[page1, page2]) as get:
            got = calendar_sync.upcoming(self.CFG, now=T)
        self.assertEqual([m["id"] for m in got], ["a", "b"])
        self.assertEqual(got[1]["call_type"], "Zoom")
        first, second = get.call_args_list
        p = first.kwargs["params"]
        self.assertEqual(p["timeMin"], "2026-10-07T13:30:00Z")
        self.assertEqual(p["timeMax"], "2026-10-08T02:30:00Z")      # CALENDAR_WINDOW_H = 12
        self.assertNotIn("pageToken", p)
        self.assertEqual(second.kwargs["params"]["pageToken"], "p2")

    def test_window_follows_the_constant(self):
        with patch.object(calendar_sync, "_access_token", return_value="at"), \
             patch.object(calendar_sync, "CALENDAR_WINDOW_H", 24), \
             patch.object(calendar_sync.requests, "get", return_value=_resp(200, {"items": []})) as get:
            self.assertEqual(calendar_sync.upcoming(self.CFG, now=T), [])
        self.assertEqual(get.call_args.kwargs["params"]["timeMax"], "2026-10-08T14:30:00Z")

    def test_a_stale_access_token_is_refreshed_once(self):
        with patch.object(calendar_sync, "_access_token", return_value="at") as tok, \
             patch.object(calendar_sync.requests, "get",
                          side_effect=[_resp(401, {}), _resp(200, {"items": [_event()]})]):
            self.assertEqual(len(calendar_sync.upcoming(self.CFG, now=T)), 1)
        self.assertEqual(tok.call_count, 2)

    def test_one_unreadable_event_does_not_blank_the_list(self):
        page = _resp(200, {"items": [_event(id="a"), _event(id="odd", start="soon"),
                                     _event(id="b", hangoutLink="https://meet.jit.si/room",
                                            conferenceData={"conferenceSolution": "x"})]})
        with patch.object(calendar_sync, "_access_token", return_value="at"), \
             patch.object(calendar_sync.requests, "get", return_value=page):
            got = calendar_sync.upcoming(self.CFG, now=T)
        self.assertEqual([m["id"] for m in got], ["a", "b"])
        self.assertEqual(got[1]["call_type"], "other")

    def test_offline_text_is_plain_and_the_same_every_time(self):
        # A requests error's text carries object addresses; the service
        # reports an error once per streak by comparing the text.
        class Offline(Exception):        # stands in for requests' errors (tests/test_core stubs requests)
            pass

        def boom(*a, **kw):
            raise Offline(f"<urllib3 connection object at {id(object()):#x}>")

        msgs = set()
        for _ in range(2):
            with patch.object(calendar_sync.requests, "RequestException", Offline), \
                 patch.object(calendar_sync, "_access_token", return_value="at"), \
                 patch.object(calendar_sync.requests, "get", side_effect=boom):
                with self.assertRaises(calendar_sync.CalendarError) as ctx:
                    calendar_sync.upcoming(self.CFG, now=T)
            msgs.add(str(ctx.exception))
        self.assertEqual(msgs, {calendar_sync.CALENDAR_OFFLINE_TEXT})
        with patch.object(calendar_sync.requests, "RequestException", Offline), \
             patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync.requests, "post", side_effect=boom):
            with self.assertRaises(calendar_sync.CalendarError) as ctx:
                calendar_sync._access_token(self.CFG)
        self.assertEqual(str(ctx.exception), calendar_sync.OFFLINE_TEXT)

    def test_an_unreadable_refresh_answer_is_a_plain_error(self):
        bad = _resp(200, None)
        bad.json.side_effect = ValueError("not json")
        with patch.object(calendar_sync.storage, "read_secret", return_value="rt"), \
             patch.object(calendar_sync.requests, "post", return_value=bad):
            with self.assertRaises(calendar_sync.CalendarError) as ctx:
                calendar_sync._access_token(self.CFG)
        self.assertNotIsInstance(ctx.exception, calendar_sync.CalendarAuthError)

    def test_errors(self):
        with patch.object(calendar_sync, "_access_token", return_value="at"), \
             patch.object(calendar_sync.requests, "get", return_value=_resp(500, {})):
            with self.assertRaises(calendar_sync.CalendarError):
                calendar_sync.upcoming(self.CFG, now=T)
        bad = _resp(200, None)
        bad.json.side_effect = ValueError("not json")
        with patch.object(calendar_sync, "_access_token", return_value="at"), \
             patch.object(calendar_sync.requests, "get", return_value=bad):
            with self.assertRaises(calendar_sync.CalendarError):
                calendar_sync.upcoming(self.CFG, now=T)


if __name__ == "__main__":
    unittest.main()
