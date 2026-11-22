"""Invite friends on screen (ui/pages/invite.py, ui/pages/invite_join.py, ui/invite_hub.py, ui/components/celebrate.py):
the page in every state (a friend counts as soon as they join), the person's own code, "Got a friend's code?" (any
time, once), the sidebar's dots and READY pill, the claim moment (Pro on, or a subscriber's next bill free), the
invited friend's card on Home and Account, the tray row, the Pro window's lines, Settings' privacy line - and no call
to the server ever made on the GUI thread. The server is a fake."""
import threading
import time
import uuid

import pytest
from PySide6.QtCore import QElapsedTimer
from PySide6.QtGui import QGuiApplication
from PySide6.QtWidgets import QApplication

from tests_qt.conftest import make_stub_app

LINK = "https://gusan.aibuben.xyz/i/aram-7k2q"


def F(name, status="counted", joined="2026-11-12T10:00:00Z", reason=""):
    return {"name": name, "initial": name[:1], "status": status, "joined_at": joined, "reason": reason}


MARIAM = F("Mariam")
DAVIT = F("Davit", joined="2026-11-16T10:00:00Z")
NARE = F("Nare", joined="2026-11-17T10:00:00Z")
DAVIT_C, NARE_C = DAVIT, NARE                     # (every friend who joined counts)


def ME(friends=(), rewards=(), **kw):
    data = {"code": "aram-7k2q", "link": LINK, "first_name": "Aram",
            "counted": sum(1 for f in friends if f["status"] == "counted"), "joined_total": len(friends),
            "rewards_left_this_year": 12, "friends": list(friends), "rewards": list(rewards), "gift": None,
            "invited_by": None, "can_join": False}
    data.update(kw)
    return data


READY = ME([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "ready"}])
ACTIVE = ME([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "active", "starts_at": "2026-11-18T10:00:00Z",
                                    "ends_at": "2026-12-18T10:00:00Z"}])
CREDITED = ME([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "credited"}])   # a subscriber's: the next bill free
BANKED = CREDITED                                 # (older name, kept for screens that still ask for it)


class Resp:
    def __init__(self, data, status=200):
        self._d, self.status_code = data, status

    def json(self):
        return self._d


class FakeServer:
    """The invite function: answers by action; records each call and whether it came on the GUI thread."""

    def __init__(self):
        self.me = ME()
        self.answers = {"hint": {"found": False}, "join": {"ok": True, "name": "Aram",
                                                          "gift_ends_at": "2026-12-02T10:00:00Z"},
                        "claim": {"ok": True, "status": "active", "starts_at": "2026-11-18T10:00:00Z",
                                  "ends_at": "2026-12-18T10:00:00Z"}}
        self.calls = []
        self.on_gui_thread = []
        self.after = {}                     # action -> callable run after answering (e.g. the next "me")
        self.statuses = {}                  # action -> the HTTP status to answer with (default 200)
        self.delay = 0.0

    def __call__(self, url, json=None, headers=None, timeout=None):
        action = json.get("action")
        gui = threading.current_thread() is threading.main_thread()
        self.calls.append(dict(json))
        self.on_gui_thread.append((action, gui))
        if self.delay:
            time.sleep(self.delay)
        data = self.me if action == "me" else self.answers.get(action, {"error": "unknown_action"})
        fn = self.after.get(action)
        if fn:
            fn()
        return Resp(data, self.statuses.get(action, 200 if action in ("me", "hint", "join", "claim") else 400))

    def actions(self):
        return [c["action"] for c in self.calls]


@pytest.fixture
def server(monkeypatch):
    import invites
    from ui.components import celebrate
    s = FakeServer()
    monkeypatch.setattr(invites, "_post", s)
    monkeypatch.setattr(invites, "device_hash", lambda: "d" * 64)
    celebrate.set_motion(False)
    yield s
    celebrate.set_motion(None)
    assert "activity" not in s.actions()                                  # gone: a friend counts when they join


@pytest.fixture
def sync(monkeypatch):
    from ui import invite_hub
    monkeypatch.setattr(invite_hub.InviteHub, "sync_default", True)


def stub(tier="free", plan=None, user=None):
    app = make_stub_app(tier)
    app.auth.user_id = user or "u-" + uuid.uuid4().hex[:10]          # each test its own account in the cache
    app.auth.get_access_token = lambda: "token"
    app.auth.plan = plan
    app.entitlement_refreshes = []
    app.auth.refresh_entitlement = lambda: app.entitlement_refreshes.append(threading.current_thread().name)
    app.opened = []
    app.show_main = lambda route=None, **k: app.opened.append(route)
    app.hints = []
    app.show_tray_hint = lambda title, msg, on_click=None: app.hints.append((title, msg))
    return app


@pytest.fixture
def window(qapp):
    from ui.shell import MainWindow
    made = []

    def build(app, w=1280, h=888):
        win = MainWindow(app)
        made.append(win)
        win.show()
        spin()
        win.setMinimumSize(w, h)
        win.resize(w, h)
        spin()
        return win

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def spin(n=6):
    for _ in range(n):
        QApplication.processEvents()


def wait_for(cond, ms=4000):
    t = QElapsedTimer()
    t.start()
    while t.elapsed() < ms:
        QApplication.processEvents()
        if cond():
            return True
        time.sleep(0.005)
    return cond()


def open_invite(window, server, me, tier="free", plan=None):
    server.me = me
    app = stub(tier, plan)
    win = window(app)
    page = win.open("invite")
    spin()
    return win, page, app


# ── the page, state by state ─────────────────────────────────────────────────
def test_nothing_yet(window, server, sync):
    win, page, _app = open_invite(window, server, ME())
    assert [s["kind"] for s in page.slots.slots()] == ["empty"] * 3
    assert page.ticket.state() == "locked"
    assert page.lead.text() == "Share your link or code. When 3 friends join, your month is ready."
    assert page.share.field.link() == LINK
    assert "aram-7k2q" in page.share.field.shown_text()
    assert page.share.code() == "ARAM-7K2Q" and page.share.code_label.text() == "ARAM-7K2Q"
    assert page.share.code_caption.text() == "Your code"
    assert page.friends.rows() == [] and page.friends.empty.isVisible()
    assert page.gift_chip.text() == "Your friends get 2 weeks of Pro too"
    assert not page.signin_card.isVisible()
    assert page.join_card is None                                         # can_join is false: nothing to enter
    item = win.sidebar.invite_item
    assert item.invite_state() == (0, 0, False, True)
    assert item.dots.isVisible() and not item.ready_pill.isVisible()
    assert item.is_active()                                               # the page is open


def test_a_friend_counts_as_soon_as_they_join(window, server, sync):
    win, page, _app = open_invite(window, server, ME([MARIAM, F("Ani", "not_counted", "2026-11-14T10:00:00Z", "same_computer")]))
    assert [s["kind"] for s in page.slots.slots()] == ["done", "empty", "empty"]
    assert page.lead.text() == "Mariam counts. Two more friends to go."
    assert page.ticket.state() == "locked" and "2 more friends to go" in page.ticket.accessibleName()
    rows = page.friends.rows()
    assert [(r.row["name"], r.chip.text()) for r in rows] == [("Mariam", "Counts"), ("Ani", "Doesn't count")]
    assert rows[0].row["meta"] == "Joined 12 Nov"
    assert page.friends.count_label.text() == "2 invited"
    assert win.sidebar.invite_item.dots.state() == (1, 0)
    assert "1 of 3 friends" in win.sidebar.invite_item.accessibleName()
    assert [t.text() for t in page.how.texts] == ["They get your link or code", "They sign up and join",
                                                  "Every 3 friends = 1 month of Pro"]


def test_ready_shows_the_glowing_ticket_and_the_ready_pill(window, server, sync):
    from ui.components import celebrate
    celebrate.set_motion(True)
    win, page, _app = open_invite(window, server, READY)
    assert page.ticket.state() == "ready"
    assert page.lead.text() == "3 friends joined. Your month is ready."
    assert page.ticket.claim_button.isVisible() and page.ticket.claim_button.text() == "Claim my month"
    assert page.ticket.shining()                                          # the slow shine runs while it shows
    item = win.sidebar.invite_item
    assert item.ready_pill.isVisible() and not item.dots.isVisible()
    assert item.ready_pill.glowing()
    assert "ready" in item.accessibleName()
    win.open("home")                                                      # off screen: the shine stops
    spin()
    assert not page.ticket.shining()


def test_active_and_credited(window, server, sync):
    _win, page, _app = open_invite(window, server, ACTIVE)
    assert page.ticket.state() == "active"
    assert page.ticket.accessibleName() == "Pro is on until 18 Dec."
    assert [s["kind"] for s in page.slots.slots()] == ["done"] * 3          # who earned it stays on the track
    _win, page, _app = open_invite(window, server, CREDITED, tier="pro", plan="monthly")
    assert page.ticket.state() == "credited"
    assert [w[0] for w in page.ticket.words()][1:] == ["Your next bill is free",
                                                       "A one-month credit is on your subscription"]
    assert page.lead.text() == "Your next bill is free. 3 more friends make another one."


def test_lifetime_accounts_see_what_their_friends_get(window, server, sync):
    _win, page, _app = open_invite(window, server, READY, tier="pro", plan="lifetime")
    assert page.ticket.state() == "ineligible"
    assert not page.ticket.claim_button.isVisible()
    assert page.share.isVisible()                                         # the link still works for them


def test_a_guest_gets_a_calm_sign_in(window, server, sync):
    app = stub("guest")
    win = window(app)
    page = win.open("invite")
    spin()
    assert page.signin_card.isVisible()
    assert not page.share.isVisible() and not page.friends.isVisible()
    assert page.join_card is None
    page.signin_button.click()
    assert ("show_auth_gate",) in app.calls
    assert "me" not in server.actions()                                   # nothing asked for a guest
    assert win.sidebar.invite_item.invite_state()[3] is False             # no dots before anything is known


def test_the_page_stacks_in_a_narrow_window(window, server, sync):
    _win, page, _app = open_invite(window, server, ME([MARIAM]))
    assert page.is_wide() == (True, True)
    win2 = window(stub(), 960, 700)
    win2.resize(960, 700)
    page2 = win2.open("invite")
    spin(10)
    assert page2.is_wide() == (False, False)


def test_copy_link_and_copy_code(window, server, sync):
    from ui.pages import invite as inv
    _win, page, _app = open_invite(window, server, ME())
    page.share.copy_button.click()
    assert QGuiApplication.clipboard().text() == LINK
    assert page.share.copied()
    page.share.copy_code_button.click()
    assert QGuiApplication.clipboard().text() == "ARAM-7K2Q"
    assert page.share.code_copied()
    assert wait_for(lambda: not page.share.copied() and not page.share.code_copied(), inv.COPIED_MS + 1500)


def test_share_chips_send_the_link_and_the_code(window, server, sync, monkeypatch):
    from ui.pages import invite as inv
    opened = []
    monkeypatch.setattr(inv, "open_url", opened.append)
    _win, page, _app = open_invite(window, server, ME())
    for key in ("whatsapp", "telegram", "email", "x", "linkedin"):
        page.share.chip[key].click()
    assert [u.split("?")[0].split(":")[0] for u in opened] == ["https", "https", "mailto", "https", "https"]
    assert opened[0].startswith("https://wa.me/?text=") and opened[4].startswith("https://www.linkedin.com/")
    assert all("ARAM-7K2Q" in u for u in opened[:4])                     # LinkedIn takes the link alone
    page.share.chip["more"].click()
    copied = QGuiApplication.clipboard().text()
    assert LINK in copied and "ARAM-7K2Q" in copied
    assert page.share.chip["more"].label() == "Message copied"


# ── the claim moment ─────────────────────────────────────────────────────────
def test_claiming_bursts_confetti_swaps_the_ticket_and_refreshes_the_plan(window, server, sync):
    from ui.components import celebrate
    celebrate.set_motion(True)
    win, page, app = open_invite(window, server, READY)
    server.after["claim"] = lambda: setattr(server, "me", ACTIVE)
    page.ticket.claim_button.click()
    assert "claim" in server.actions()
    assert server.calls[server.actions().index("claim")]["id"] == "r1"
    assert app.entitlement_refreshes                                      # the plan is asked again
    confetti = page._last_confetti
    assert confetti is not None and confetti.running()
    assert len(confetti.pieces) == celebrate.CONFETTI_COUNT
    colours = {p["colour"] for p in confetti.pieces}
    assert {"flag-red", "flag-blue", "flag-orange"} <= colours
    assert page.ticket.state() == "active" and page.ticket.swapping()
    toast = celebrate.current_toast(win)
    assert toast is not None and toast.title() == "Welcome to Pro"
    assert toast.sub() == "Every Pro feature is on until 18 Dec."
    done = []
    confetti.finished.connect(lambda: done.append(True))
    assert wait_for(lambda: bool(done), 3000)                             # it ends, by itself
    assert not page.ticket.swapping()
    assert win.sidebar.invite_item.invite_state()[2] is False             # no READY any more


def test_a_subscriber_gets_the_next_bill_free(window, server, sync):
    from ui.components import celebrate
    win, page, app = open_invite(window, server, READY, tier="pro", plan="monthly")
    server.answers["claim"] = {"ok": True, "status": "next_bill_free", "amount": 1599, "currency": "eur"}
    server.after["claim"] = lambda: setattr(server, "me", ME([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "used"}]))
    page.ticket.claim_button.click()
    assert page.ticket.state() == "credited"
    toast = celebrate.current_toast(win)
    assert (toast.title(), toast.sub()) == ("Your next bill is free", "A one-month credit is on your subscription.")
    assert page._last_confetti is None                                   # motion off: no confetti at all
    win.invites.refresh(force=True)                                      # the server calls it "used" later:
    spin()
    assert page.ticket.state() == "credited"                             # what happened stays said
    assert ("invite_reward_claimed", {"status": "next_bill_free"}) in _events(app)


def test_a_failed_claim_keeps_the_ticket_and_says_why(window, server, sync):
    _win, page, app = open_invite(window, server, READY)
    server.answers["claim"] = {"ok": False, "reason": "network"}
    page.ticket.claim_button.click()
    assert page.ticket.state() == "ready" and page.ticket.claim_button.isEnabled()
    assert page.claim_note.isVisible() and "connection" in page.claim_note.text()
    assert app.entitlement_refreshes == []


def test_the_card_company_busy_is_said_calmly(window, server, sync):
    from ui import theme
    _win, page, _app = open_invite(window, server, READY, tier="pro", plan="yearly")
    server.answers["claim"] = {"ok": False, "reason": "stripe_error"}
    page.ticket.claim_button.click()
    assert page.ticket.state() == "ready" and page.ticket.claim_button.isEnabled()      # try again in a minute
    assert page.claim_note.text() == "Couldn't apply it right now. Try again in a minute."
    assert theme.qss_color("muted") in page.claim_note.styleSheet()                    # calm, not red


def test_a_claim_the_server_refuses_for_a_lifetime_account(window, server, sync):
    _win, page, app = open_invite(window, server, READY, tier="pro", plan="")
    server.answers["claim"] = {"ok": False, "reason": "not_eligible"}
    page.ticket.claim_button.click()
    assert page.ticket.state() == "ineligible"
    assert page.claim_note.text() == ("Rewards are for Free and subscription plans. "
                                      "Your friends still get 2 weeks of Pro.")
    assert page.ticket.accessibleName() == page.claim_note.text()
    page.render()
    assert page.ticket.state() == "ineligible"                            # kept until the plan changes
    assert not page.lead.text().endswith("Your month is ready.")
    assert page.window().sidebar.invite_item.invite_state()[2] is False  # no READY left in the sidebar
    assert app.entitlement_refreshes == []


# ── a friend who newly counts ────────────────────────────────────────────────
def test_a_friend_who_joins_gets_one_note(window, server, sync):
    from ui.components import celebrate
    win, page, app = open_invite(window, server, ME([MARIAM]))
    assert celebrate.current_toast(win) is None                           # the first look announces nobody
    server.me = ME([MARIAM, DAVIT])
    win.invites.refresh(force=True)
    spin()
    toast = celebrate.current_toast(win)
    assert toast is not None and toast.title() == "Davit counts"
    assert toast.sub() == "1 more friend for your free month"
    toast.dismiss()
    spin()
    win.invites.refresh(force=True)
    spin()
    assert celebrate.current_toast(win) is None                           # once per friend
    win.hide()
    server.me = ME([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "ready"}])
    win.invites.refresh(force=True)
    spin()
    assert app.hints == [("Nare counts", "Your free month is ready")]     # the window is away: the tray says it


def test_names_the_server_keeps(window, server, sync):
    server.answers["hint"] = {"found": True, "code": "aram-7k2q"}            # no name
    _win, page, app = open_invite(window, server, ME(can_join=True))
    card = page.join_card
    assert card.mode == "found"
    assert card.title.text() == "A friend invited you"
    assert card.join_found.text() == "Join the invite"
    server.answers["join"] = {"ok": True, "name": None, "gift_ends_at": "2026-12-02T10:00:00Z"}
    server.after["join"] = lambda: setattr(server, "me", ME(invited_by={"name": None}))
    card.join_found.click()
    spin()
    assert card.mode == "joined"
    assert card.title.text() == "You joined the invite. Pro is on for 2 weeks."
    assert "your friend" in card.sub.text()
    unnamed = ME([{"name": "A friend", "initial": "", "status": "counted", "joined_at": "2026-11-12T10:00:00Z"}])
    _win3, page3, _app3 = open_invite(window, server, unnamed)
    assert page3.slots.slots()[0]["initial"] == "" and page3.slots.slots()[0]["name"] == "A friend"
    assert page3.lead.text().startswith("A friend counts.")


# ── the invited friend: the Invite page and Account, never Home ──────────────
def test_home_has_no_invite_card_and_asks_nothing(window, server, sync):
    from ui.pages.invite_join import CodeJoinCard
    server.me = ME(can_join=True)
    server.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    win = window(stub())
    home = win.open("home")
    spin(10)
    assert getattr(home, "join_card", None) is None
    assert home.findChildren(CodeJoinCard) == []
    assert "hint" not in server.actions() and "join" not in server.actions()
    import ui.pages.invite_join as ij
    assert not hasattr(ij, "JoinCard")                                    # the Home card is gone for good


def test_the_code_card_offers_the_invite_the_server_found(window, server, sync):
    server.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    win, page, app = open_invite(window, server, ME([MARIAM], can_join=True))
    card = page.join_card
    assert card is not None and card.isVisible() and card.mode == "found"
    assert card.title.text() == "Aram invited you"
    assert card.join_found.text() == "Join Aram's invite" and card.not_me_button.text() == "Not me"
    assert not card.box.isVisible()
    assert server.actions().count("hint") == 1                            # asked once, when it was first shown
    win.open("home")
    win.open("invite")
    spin()
    assert server.actions().count("hint") == 1
    server.after["join"] = lambda: setattr(server, "me", ME([MARIAM], invited_by={"name": "Aram"}))
    card.join_found.click()
    spin()
    join = server.calls[server.actions().index("join")]
    assert (join["code"], join["source"], join["device"]) == ("aram-7k2q", "hint", "d" * 64)
    assert app.entitlement_refreshes                                      # 2 weeks of Pro: the plan is asked again
    assert card.mode == "joined" and card.isVisible()
    assert card.title.text() == "You joined Aram's invite. Pro is on for 2 weeks."
    assert card.sub.text() == "Thanks - Aram gets closer to a free month."


def test_not_me_switches_to_the_field(window, server, sync):
    import invites
    from ui.pages.invite_join import PLACEHOLDER
    server.me = ME(can_join=True)
    server.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    win = window(stub())
    page = win.open("account")
    spin(6)
    card = page.join_card
    assert card.mode == "found"
    card.not_me_button.click()
    assert card.mode == "code" and card.title.text() == "Got a friend's code?"
    box = card.box
    assert box.isVisible() and box.field.placeholderText() == PLACEHOLDER == "Code or link"
    QGuiApplication.clipboard().setText("https://gusan.aibuben.xyz/i/nare-55xk")
    assert box.field.text() == ""                                         # the clipboard is read only on Paste
    box.paste_button.click()
    assert box.field.text() == "https://gusan.aibuben.xyz/i/nare-55xk"
    server.answers["join"] = {"ok": False, "reason": "own_code"}
    box.join_button.click()
    spin()
    assert box.error.isVisible() and box.error.text() == invites.join_error("own_code")
    assert server.calls[-1]["code"] == "nare-55xk" and server.calls[-1]["source"] == "paste"
    box.field.setText("")
    box.join_button.click()
    assert box.error.text() == invites.join_error("empty")
    assert server.actions().count("join") == 1                            # nothing sent for an empty field
    card._on_hint({"found": True, "name": "Aram", "code": "aram-7k2q"})
    assert card.mode == "code"                                            # "Not me" stays said


def test_a_hint_that_lands_late_never_replaces_what_is_typed(window, server, sync):
    _win, page, _app = open_invite(window, server, ME(can_join=True))
    card = page.join_card
    assert card.mode == "code"                                            # nothing found: just the field
    card.box.field.setText("nare-55")
    card._on_hint({"found": True, "name": "Aram", "code": "aram-7k2q"})
    assert card.mode == "code" and card.box.field.text() == "nare-55"
    card.box.field.setText("")
    card._on_hint({"found": True, "name": "Aram", "code": "aram-7k2q"})
    assert card.mode == "found"


def test_a_failed_join_of_the_found_invite_says_why(window, server, sync):
    import invites
    server.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    server.answers["join"] = {"ok": False, "reason": "seen_before"}
    _win, page, app = open_invite(window, server, ME(can_join=True))
    card = page.join_card
    card.join_found.click()
    spin()
    assert card.mode == "found" and card.join_found.isEnabled()
    assert card.found_error.isVisible() and card.found_error.text() == invites.join_error("seen_before")
    assert app.entitlement_refreshes == []


def test_got_a_friends_code_on_the_invite_page(window, server, sync):
    win, page, app = open_invite(window, server, ME([MARIAM], can_join=True))
    card = page.join_card
    assert card is not None and card.isVisible() and card.mode == "code"
    assert card.title.text() == "Got a friend's code?"
    assert card.sub.text() == "Enter it once, any time, and get 2 weeks of Pro."
    server.after["join"] = lambda: setattr(server, "me", ME([MARIAM], invited_by={"name": "Nare"}))
    server.answers["join"] = {"ok": True, "name": "Nare", "gift_ends_at": "2026-12-02T10:00:00Z"}
    card.box.field.setText("NARE-55XK")                                   # typed in upper case: it works the same
    card.box.join_button.click()
    spin()
    join = server.calls[server.actions().index("join")]
    assert (join["code"], join["source"]) == ("nare-55xk", "paste")
    assert card.mode == "joined" and card.title.text() == "You joined Nare's invite. Pro is on for 2 weeks."
    assert page.join_card is card and card.isVisible()                    # the answer stays for this run
    _win2, page2, _app2 = open_invite(window, server, ME([MARIAM], invited_by={"name": "Nare"}))
    assert page2.join_card is None                                        # joined once: nothing more to enter


def test_account_has_got_a_friends_code_while_it_can_join(window, server, sync):
    server.me = ME(can_join=True)
    app = stub()
    win = window(app)
    page = win.open("account")
    spin()
    assert page.join_card is not None and page.join_card.isVisible()
    assert page.join_card.mode == "code" and page.join_card.title.text() == "Got a friend's code?"
    server.after["join"] = lambda: setattr(server, "me", ME(invited_by={"name": "Aram"}))
    page.join_card.box.field.setText("aram-7k2q")
    page.join_card.box.join_button.click()
    spin()
    card = page.join_card                                                 # (the plan change redrew the page)
    assert card.mode == "joined" and card.title.text() == "You joined Aram's invite. Pro is on for 2 weeks."
    server.me = ME()
    app2 = stub()
    win2 = window(app2)
    assert win2.open("account").join_card is None


def test_pro_from_invites_wears_its_days_in_the_sidebar(window, server, sync):
    from datetime import datetime, timedelta, timezone
    app = stub("pro", plan="invite")
    app.auth.period_end = (datetime.now(timezone.utc) + timedelta(days=13, hours=12)).isoformat()
    win = window(app)
    assert win.sidebar.account_row.badge.text() == "PRO · 14 DAYS"
    app.auth.plan = "monthly"
    win.refresh_account()
    assert win.sidebar.account_row.badge.text() == "PRO"


# ── elsewhere ────────────────────────────────────────────────────────────────
def test_the_tray_row(qapp, server, sync):
    from PySide6.QtWidgets import QMenu
    from ui import invite_hub
    from ui import tray_menu as tm
    app = stub()
    tr = tm.build_tray_menu(app, QMenu(), custom=True)
    assert dict(tr.visible_texts())["invite"] == "Invite friends"
    assert tr.actions["invite"].property(tm.P_DOTS) == ""                # no hub yet: never made by the tray
    assert getattr(app, "invite_hub", None) is None
    server.me = READY
    hub = invite_hub.hub(app)
    hub.refresh(force=True)
    tr.refresh()
    assert tr.actions["invite"].property(tm.P_DOTS) == "ready"
    tr.actions["invite"].trigger()
    assert app.opened == ["invite"]
    mac = tm.build_tray_menu(app, QMenu(), custom=False)
    assert dict(mac.visible_texts())["invite"].endswith("Ready")
    guest = tm.build_tray_menu(stub("guest"), QMenu(), custom=True)
    assert "invite" not in dict(guest.visible_texts())


def test_the_pro_window_has_its_quiet_lines(qapp):
    from ui.pro_dialog import INVITE_LINE, PROMO_LINE, ProDialog
    app = stub()
    dlg = ProDialog(app)
    assert dlg.invite_line.text() == INVITE_LINE == "Or get it free: invite 3 friends"
    assert dlg.promo_note.text() == PROMO_LINE == "Have a promo code? Add it at checkout."
    for plan, shown in (("yearly", True), ("monthly", True), ("lifetime", False)):
        dlg._select_plan(plan, move=False)
        assert dlg.promo_note.isVisibleTo(dlg) is shown, plan                # promo codes: Monthly and Yearly only
    dlg.invite_line.click()
    assert app.opened == ["invite"]
    dlg.deleteLater()
    guest = ProDialog(stub("guest"))
    assert not guest.promo_note.isVisibleTo(guest)                        # a guest signs in first
    guest.deleteLater()


def test_settings_privacy_says_what_invites_use(window, server, sync):
    import invites
    win = window(stub())
    page = win.open("settings", section="privacy")
    page.ensure_built()
    spin()
    assert page.row_invites.hint_label.text() == invites.PRIVACY_LINE
    assert invites.PRIVACY_LINE == "Only when you join an invite: a one-way code of this computer. " \
                                   "Never what you say or type."


# ── never on the GUI thread ──────────────────────────────────────────────────
def test_no_call_to_the_server_runs_on_the_gui_thread(window, server, monkeypatch):
    from ui import invite_hub
    monkeypatch.setattr(invite_hub, "REFRESH_AFTER_SIGN_IN_MS", 0)
    server.delay = 0.05
    server.me = READY
    app = stub()
    win = window(app)
    hub = win.invites
    assert not hub.sync_io
    page = win.open("invite")
    assert wait_for(lambda: page.ticket.state() == "ready")
    server.after["claim"] = lambda: setattr(server, "me", ACTIVE)
    page.ticket.claim_button.click()
    assert page.ticket.claim_button.text() == "Claiming…"                 # the window answers while it waits
    assert wait_for(lambda: page.ticket.state() == "active")
    assert app.entitlement_refreshes and all(n != threading.main_thread().name for n in app.entitlement_refreshes)
    # the invited friend's calls too
    server.me = ME(can_join=True)
    server.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    app2 = stub()
    win2 = window(app2)
    assert wait_for(lambda: win2.invites.can_join())
    account = win2.open("account")
    assert wait_for(lambda: account.join_card is not None and account.join_card.mode == "found")
    server.after["join"] = lambda: setattr(server, "me", ME(invited_by={"name": "Aram"}))
    account.join_card.join_found.click()
    assert wait_for(lambda: account.join_card is not None and account.join_card.mode == "joined")
    actions = {a for a, _gui in server.on_gui_thread}
    assert {"me", "claim", "hint", "join"} <= actions
    assert [a for a, gui in server.on_gui_thread if gui] == []


# ── the funnel's usage events ────────────────────────────────────────────────
def _events(app):
    return [(c[1], c[2]) for c in app.calls if c and c[0] == "track" and str(c[1]).startswith("invite_")]


def test_the_funnel_is_counted_without_codes_links_or_names(window, server, sync, monkeypatch):
    from ui.pages import invite as inv
    monkeypatch.setattr(inv, "open_url", lambda url: None)
    _win, page, app = open_invite(window, server, READY)
    assert ("invite_page_opened", {"signed_in": True}) in _events(app)
    page.share.copy_button.click()
    for key in ("whatsapp", "telegram", "email", "x", "linkedin", "more"):
        page.share.chip[key].click()
    assert [p["channel"] for e, p in _events(app) if e == "invite_link_shared"] == \
        ["copy", "whatsapp", "telegram", "email", "x", "linkedin", "more"]
    server.after["claim"] = lambda: setattr(server, "me", ACTIVE)
    page.ticket.claim_button.click()
    assert ("invite_reward_claimed", {"status": "active"}) in _events(app)
    said = repr(_events(app))
    assert "aram-7k2q" not in said.lower() and "gusan.aibuben.xyz" not in said and "Mariam" not in said
    assert "r1" not in said
    import telemetry
    assert {e for e, _p in _events(app)} <= telemetry.ALLOWED_EVENTS


def test_joining_is_counted_by_how_it_came(window, server, sync):
    server.me = ME(can_join=True)
    server.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    app = stub()
    win = window(app)
    page = win.open("invite")
    spin(10)
    server.after["join"] = lambda: setattr(server, "me", ME(invited_by={"name": "Aram"}))
    page.join_card.join_found.click()
    spin()
    assert [p for e, p in _events(app) if e == "invite_joined"] == [{"source": "hint"}]
    server.me = ME(can_join=True)
    server.answers["hint"] = {"found": False}
    server.answers["join"] = {"ok": False, "reason": "own_code"}
    app2 = stub()
    win2 = window(app2)
    win2.invites.refresh(force=True)
    page = win2.open("account")
    spin()
    page.join_card.box.field.setText("aram-7k2q")
    page.join_card.box.join_button.click()
    spin()
    assert [e for e, _p in _events(app2)] == []                          # a join that failed is not counted
    server.answers["join"] = {"ok": True, "name": "Aram", "gift_ends_at": "2026-12-02T10:00:00Z"}
    server.after["join"] = lambda: setattr(server, "me", ME(invited_by={"name": "Aram"}))
    page.join_card.box.join_button.click()
    spin()
    assert [p for e, p in _events(app2) if e == "invite_joined"] == [{"source": "paste"}]


# ── what the server added on 2026-11-20 ──────────────────────────────────────
CREDITING = ME([MARIAM, DAVIT, NARE], [{"id": "r1", "status": "crediting"}])


def test_a_month_being_applied_shows_a_busy_ticket_then_turns_credited(window, server, sync, monkeypatch):
    from ui.pages import invite as inv
    monkeypatch.setattr(inv, "CREDITING_POLL_MS", 30)
    win, page, _app = open_invite(window, server, CREDITING, tier="pro", plan="monthly")
    assert page.ticket.state() == "crediting"
    b = page.ticket.claim_button
    assert b.isVisible() and not b.isEnabled() and b.text() == "Applying your free month…"
    assert page.lead.text() == "3 friends joined. Applying your free month…"
    assert win.sidebar.invite_item.invite_state()[2] is False             # nothing to claim: no READY pill
    asked = server.actions().count("me")
    server.me = CREDITED                                                  # the server finished applying it
    assert wait_for(lambda: page.ticket.state() == "credited", 3000)      # the page asked again by itself
    assert server.actions().count("me") > asked
    assert not page.ticket.claim_button.isVisible()


def test_a_claim_that_errors_after_the_card_company_said_yes(window, server, sync):
    from ui import theme
    _win, page, _app = open_invite(window, server, READY, tier="pro", plan="yearly")
    server.answers["claim"] = {"error": "server_error"}
    server.statuses["claim"] = 500
    server.after["claim"] = lambda: setattr(server, "me", CREDITED)       # Stripe said yes before the error
    page.ticket.claim_button.click()
    spin()
    assert page.claim_note.isVisible() and "Check again in a minute" in page.claim_note.text()
    assert theme.qss_color("muted") in page.claim_note.styleSheet()                    # calm, not red
    assert server.actions()[-1] == "me"                                   # asked again at once
    assert page.ticket.state() == "credited"                              # ...and it shows the month


def test_new_join_reasons_are_said_plainly(window, server, sync):
    import invites
    _win, page, _app = open_invite(window, server, ME(can_join=True))
    box = page.join_card.box
    for reason in ("same_network", "disposable_email", "limit"):
        server.answers["join"] = {"ok": False, "reason": reason}
        box.field.setText("aram-7k2q")
        box.join_button.click()
        spin()
        assert box.error.text() == invites.join_error(reason)
    assert box.error.text() == "This invite can't take more friends right now. Try again later."
