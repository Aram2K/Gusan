"""The account card by plan (sidebar, tray, Account & plan, Settings link, Pro window).

A guest - not signed in, or a super admin previewing Guest (signed in underneath, so the session still
holds a profile name and email) - is "User" with a person icon and ONE blue "Sign in / Sign up". They never
see a person's name, an initial, an email or any upgrade wording. Free keeps its quiet violet "Upgrade to
Pro"; Pro and a trial show the PRO pill and nothing to buy."""
import pytest
from PySide6.QtWidgets import QAbstractButton, QApplication, QDialog, QLabel, QMenu, QWidget

from tests_qt.conftest import make_stub_app

STALE_NAME = "Aram Adamyan"
STALE_EMAIL = "aram@aibuben.xyz"


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()


def stale_guest_app():
    """A super admin previewing Guest: the tier is guest (cfg override), the session is still the admin's."""
    app = make_stub_app("guest")
    a = app.auth
    a.is_authenticated = True
    a.is_admin = True
    a.user_id = "u1"
    a.plan = "admin"
    a.user_name = STALE_NAME
    a.user_email = STALE_EMAIL
    a.get_access_token = lambda: "token"
    app.cfg["admin_tier_override"] = "guest"
    return app


def trial_app():
    app = make_stub_app("pro")
    app.auth.plan = "trial"
    app.auth.subscription_status = "trialing"
    app.auth.period_end = "2099-01-01T00:00:00Z"
    app.auth.user_id = "u1"
    return app


APPS = {"guest": lambda: make_stub_app("guest"), "guest_preview": stale_guest_app,
        "free": lambda: make_stub_app("free"), "trial": trial_app, "pro": lambda: make_stub_app("pro")}


@pytest.fixture
def build(qapp, monkeypatch):
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    made = []

    def _build(kind="guest"):
        from ui.shell import MainWindow
        app = APPS[kind]()
        win = MainWindow(app)
        win.resize(1280, 820)
        made.append(win)
        return win, app
    yield _build
    for w in made:
        w.hide()
        w.deleteLater()
    _spin()


def _texts(root):
    """Every word a person could read or a screen reader says, under ``root`` (visible or not)."""
    out = []
    for w in root.findChildren(QWidget) + [root]:
        if isinstance(w, (QLabel, QAbstractButton)):
            out.append(w.text())
        out += [w.accessibleName(), w.accessibleDescription(), w.toolTip()]
    return [t for t in out if t]


# ── the sidebar card ──
def test_the_guest_card_is_a_person_icon_user_and_one_blue_button(build):
    from ui.components import GuestBadge
    win, app = build("guest")
    sb = win.sidebar
    row = sb.account_row
    assert sb.tier() == "guest"
    assert row.name_label.text() == "User"
    assert row.avatar.is_guest() and row.avatar.letter() == ""           # a person icon, no letter
    assert isinstance(row.badge, GuestBadge)
    assert row.accessibleName() == "Account: User, Guest"
    btn = sb.sign_in_button
    assert btn is not None and btn.text() == "Sign in / Sign up"
    assert btn.accessibleName() == "Sign in or sign up"
    assert btn.kind() == "primary"                                       # the app's blue, like the other blue buttons
    assert sb.upgrade_button is None
    assert not [t for t in _texts(sb) if "upgrade" in t.lower() or "pro" == t.lower()]


def test_the_free_card_keeps_the_quiet_violet_upgrade(build):
    from ui.components import FreeBadge
    win, _ = build("free")
    sb = win.sidebar
    assert sb.tier() == "free" and sb.account_row.name_label.text() == "Aram"
    assert sb.account_row.avatar.letter() == "A" and not sb.account_row.avatar.is_guest()
    assert isinstance(sb.account_row.badge, FreeBadge)
    assert sb.upgrade_button.text() == "Upgrade to Pro" and sb.upgrade_button.property("gusan") == "upgradeQuiet"
    assert sb.sign_in_button is None                                     # no sign-in wording for someone signed in
    assert not [t for t in _texts(sb) if "sign in" in t.lower() or "sign up" in t.lower()]


@pytest.mark.parametrize("kind", ["pro", "trial"])
def test_pro_and_a_trial_show_the_name_and_the_pro_pill_only(build, kind):
    from ui.components import ProTag
    win, _ = build(kind)
    sb = win.sidebar
    assert sb.tier() == "pro" and isinstance(sb.account_row.badge, ProTag)
    assert sb.account_row.name_label.text() == "Aram" and sb.account_row.avatar.letter() == "A"
    assert sb.upgrade_button is None and sb.sign_in_button is None       # calm Pro
    assert not [t for t in _texts(sb) if "upgrade" in t.lower() or "sign in" in t.lower()]


def test_the_card_follows_the_plan_both_ways(build):
    win, app = build("free")
    app.set_tier("guest")
    win.refresh_account()
    assert win.sidebar.sign_in_button is not None and win.sidebar.upgrade_button is None
    assert win.sidebar.account_row.name_label.text() == "User"
    app.set_tier("free")
    win.refresh_account()
    assert win.sidebar.sign_in_button is None and win.sidebar.upgrade_button is not None
    assert win.sidebar.account_row.name_label.text() == "Aram" and win.sidebar.account_row.avatar.letter() == "A"


def test_sign_in_and_upgrade_are_different_signals(qapp):
    from ui.components import Sidebar
    sb = Sidebar()
    got = []
    sb.sign_in.connect(lambda: got.append("sign_in"))
    sb.upgrade.connect(lambda: got.append("upgrade"))
    sb.set_account("guest", "whatever")
    sb.sign_in_button.click()
    sb.set_account("free", "Aram")
    sb.upgrade_button.click()
    assert got == ["sign_in", "upgrade"]
    sb.deleteLater()


def test_the_sidebar_ignores_a_name_handed_in_for_a_guest(qapp):
    from ui.components import Sidebar
    sb = Sidebar()
    sb.set_account("guest", STALE_NAME)                                  # no caller can put a person on the card
    assert sb.account_row.name_label.text() == "User" and sb.account_row.avatar.letter() == ""
    assert STALE_NAME not in " ".join(_texts(sb))
    sb.deleteLater()


def test_the_avatar_icon_replaces_the_letter(qapp):
    from ui.components import Avatar
    person, guest = Avatar("Aram", 44), Avatar("Aram", 44, guest=True)
    assert person.letter() == "A" and guest.letter() == "" and guest.is_guest()
    assert person.grab().toImage() != guest.grab().toImage()             # a different picture, not the letter
    guest.set_name("Aram")                                               # a new name does not give a guest a letter
    assert guest.is_guest() and guest.letter() == ""
    guest.set_name("Aram", guest=False)
    assert guest.letter() == "A"


# ── a stale profile never reaches a guest ──
def test_a_stale_profile_never_reaches_a_guest_anywhere(build):
    import account_actions
    from ui.shell import ROUTES, account_state
    win, app = build("guest_preview")
    assert app.auth.is_authenticated and app.auth.user_name == STALE_NAME          # the profile is still there
    assert account_state(app) == ("guest", "User")
    assert account_actions.first_name(app) == "" and account_actions.is_guest(app)
    win.show()
    sb = win.sidebar
    assert sb.tier() == "guest" and sb.account_row.name_label.text() == "User"
    assert sb.account_row.avatar.letter() == "" and sb.sign_in_button is not None and sb.upgrade_button is None
    for route in ROUTES:                                                 # every page, every word
        page = win.open(route)
        _spin(8)
        for text in _texts(win):
            low = text.lower()
            assert "aram" not in low and STALE_EMAIL not in low, (route, text)
    assert win.page("home").header.title_label.text() == "Welcome"       # not "Welcome, Aram"
    app.set_tier("free")                                                 # the same session as a Free user: the name
    app.cfg["admin_tier_override"] = "auto"
    win.refresh_account()
    assert win.sidebar.account_row.name_label.text() == "Aram"


def test_the_settings_link_wears_the_person_icon_for_a_guest(build):
    win, app = build("guest_preview")
    page = win.open("settings")
    page.ensure_built()
    _spin()
    avatar = page.nav.account_link.avatar
    assert avatar.is_guest() and avatar.letter() == ""
    app.set_tier("free")
    app.cfg["admin_tier_override"] = "auto"
    page.refresh_account()
    assert not avatar.is_guest() and avatar.letter() == "A"


# ── Account & plan ──
@pytest.mark.parametrize("kind", ["guest", "guest_preview"])
def test_the_account_page_for_a_guest(build, kind):
    win, app = build(kind)
    acct = win.open("account")
    assert acct.name_label.text() == "User" and acct.email_label.text() == "Not signed in"
    assert acct.avatar.is_guest() and acct.avatar.letter() == ""
    assert acct.plan_name.text() == "Guest"
    assert "upgrade" not in acct.buttons and "sign_out" not in acct.buttons and acct.delete_card is None
    btn = acct.buttons["sign_in"]
    assert btn.text() == "Sign in / Sign up" and btn.accessibleName() == "Sign in or sign up"
    assert btn.kind() == "primary"
    assert acct.guest_note is not None and "Sign in first" in acct.guest_note.text()
    assert not [t for t in _texts(acct.plan_card) if "upgrade" in t.lower() or "€" in t]    # no price, no upgrade
    assert STALE_NAME not in " ".join(_texts(acct)) and STALE_EMAIL not in " ".join(_texts(acct))
    btn.click()
    assert ("show_auth_gate",) in app.calls
    assert not [c for c in app.calls if c[0] == "_pro_upsell"]
    if kind == "guest_preview":
        assert acct.admin_segmented is not None                          # the way back stays


def test_the_account_page_keeps_upgrade_for_free_only(build):
    for kind, has_upgrade in (("free", True), ("pro", False), ("trial", False)):
        win, _ = build(kind)
        acct = win.open("account")
        assert ("upgrade" in acct.buttons) is has_upgrade, kind
        assert "sign_in" not in acct.buttons, kind
        if has_upgrade:
            assert acct.buttons["upgrade"].text() == "Upgrade to Pro"


# ── the tray ──
@pytest.fixture
def tray(qapp):
    from ui import tray_menu as tm
    made = []

    def _tray(kind):
        app = APPS[kind]()
        for name in ("show_main", "quit_app", "_on_hotkey", "show_tray_hint", "show_auth_gate"):
            setattr(app, name, (lambda n: lambda *a, **k: app.calls.append((n,) + a))(name))
        tr = tm.build_tray_menu(app, QMenu(), custom=True)
        tr.refresh()
        made.append(tr)
        return tr, app
    yield _tray
    for tr in made:
        tr.menu.hide()
        tr.menu.deleteLater()


@pytest.mark.parametrize("kind", ["guest", "guest_preview"])
def test_the_tray_header_for_a_guest(tray, kind):
    from ui import tray_menu as tm
    tr, app = tray(kind)
    rows = dict(tr.visible_texts())
    assert rows["account"] == "Guest"                                    # no email, no plan, no name
    assert rows["signin"] == "Sign in / Sign up" and "upgrade" not in rows
    assert tr.actions["signin"].property(tm.P_KIND) == "signin"
    assert tr.tooltip() == "Gusan"                                       # no email in the icon's tooltip either
    assert STALE_EMAIL not in " ".join(t for _k, t in tr.visible_texts())
    tr.actions["signin"].trigger()
    assert ("show_auth_gate",) in app.calls


def test_the_tray_rows_for_free_pro_and_trial(tray):
    tr, _ = tray("free")
    rows = {k: t.replace(" ", " ") for k, t in tr.visible_texts()}
    assert rows["account"] == STALE_EMAIL + "  ·  Free" and rows["upgrade"] == "Upgrade to Pro…"
    assert "signin" not in rows
    for kind in ("pro", "trial"):
        tr, _ = tray(kind)
        rows = dict(tr.visible_texts())
        assert rows["account"].endswith("Pro") and "upgrade" not in rows and "signin" not in rows


# ── the Pro window ──
def _pro_dialog(app):
    from ui.pro_dialog import ProDialog
    from ui.styles import STYLESHEET
    app.style_content = STYLESHEET
    return ProDialog(main_app=app, feature="Meeting recording")


def test_the_pro_window_for_a_guest_leads_through_sign_in(qapp, monkeypatch):
    import webbrowser
    opened = []
    monkeypatch.setattr(webbrowser, "open", lambda url, *a, **k: opened.append(url))
    for app in (make_stub_app("guest"), stale_guest_app()):
        dlg = _pro_dialog(app)
        assert dlg.btn_go.text() == "Sign in to upgrade"
        assert dlg.note.text() == "Sign in first, then try Pro free for 3 days."
        assert "a855f7" not in dlg.btn_go.styleSheet()                    # the blue sign-in button, not the violet trial one
        dlg.btn_go.click()
        assert ("show_auth_gate",) in app.calls
        assert opened == []                                              # no checkout before there is an account
        assert dlg.result() == QDialog.DialogCode.Accepted
        dlg.deleteLater()


def test_the_pro_window_for_free_still_starts_the_trial(qapp):
    dlg = _pro_dialog(make_stub_app("free"))
    assert dlg.btn_go.text() == "Start 3-day free trial"
    assert "a855f7" in dlg.btn_go.styleSheet()
    assert "Stripe" in dlg.note.text() and "Sign in" not in dlg.note.text()
    dlg.deleteLater()


# ── the pages that name a person ──
def test_the_example_email_sign_off_is_never_a_guests_profile(build):
    from ui.pages import live_assistant, smart_actions
    for kind, expected in (("guest", ""), ("guest_preview", ""), ("free", "Aram")):
        _win, app = build(kind)
        assert smart_actions._first_name(app) == expected, kind
        assert live_assistant._first_name(app) == expected, kind
