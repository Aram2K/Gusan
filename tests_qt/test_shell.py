"""The Gusan main window (ui/shell.py) builds, routes and survives broken pages."""
import pytest
from PySide6.QtCore import Qt
from PySide6.QtGui import QGuiApplication
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QAbstractButton, QApplication, QWidget

from tests_qt.conftest import make_stub_app


@pytest.fixture
def shell(qapp):
    from ui import shell as shell_mod
    made = []

    def build(tier="free", **kw):
        app = make_stub_app(tier, **kw)
        win = shell_mod.MainWindow(app)
        made.append(win)
        return win, app

    yield build
    for w in made:
        w.hide()
        w.deleteLater()
    QApplication.processEvents()


def test_window_basics(shell):
    from ui.shell import DEFAULT_SIZE
    win, _ = shell()
    assert win.windowTitle() == "Gusan"
    assert win.pages() == {}                     # lazy: nothing built until opened
    assert win.current_route() is None
    assert (win.width(), win.height()) == DEFAULT_SIZE or win.width() <= DEFAULT_SIZE[0]
    assert win.sidebar.width() == 232


def test_every_route_opens_without_an_error_card(shell):
    from ui.shell import ROUTES, ErrorPage
    win, app = shell()
    win.show()
    for route in ROUTES:
        page = win.open(route)
        QApplication.processEvents()
        assert isinstance(page, QWidget)
        assert not isinstance(page, ErrorPage), route
        assert win.current_route() == route
        assert win.page(route) is page
        assert not hasattr(page, "classic_button"), route        # no link to a 1.x window
    for route in ROUTES:
        if route in win.sidebar.items:
            win.open(route)
            assert win.sidebar.items[route].is_active()
            others = [r for r, it in win.sidebar.items.items() if it.is_active()]
            assert others == [route]
    tracked = [c for c in app.calls if c[0] == "track"]
    assert ("track", "settings_tab_opened", {"tab": "home"}) in tracked


def test_reopening_a_route_reuses_the_page_and_passes_options(shell):
    win, _ = shell()
    first = win.open("settings", section="speech")
    assert first.opts == {"section": "speech"}
    again = win.open("settings", section="privacy")
    assert again is first and again.opts == {"section": "privacy"}


def test_unknown_route_opens_home(shell):
    win, _ = shell()
    win.open("nope")
    assert win.current_route() == "home"


def test_placeholder_titles(shell):
    win, _ = shell("free")
    assert win.open("home").header.title_label.text() == "Welcome, Aram"
    assert win.open("audio_to_text").header.title_label.text() == "Convert audio to text"
    assert win.open("account").header.title_label.text() == "Account & plan"
    guest, _ = shell("guest")
    assert guest.open("home").header.title_label.text() == "Welcome"


@pytest.mark.parametrize("route,method", [
    ("settings", "show_settings"), ("meetings", "show_meeting"),
    ("audio_to_text", "show_file_transcribe"), ("home", "show_history"),
    ("live_assistant", "toggle_live_assist"),
])
def test_placeholders_link_to_the_classic_windows(qapp, monkeypatch, route, method):
    # Wave 4: the classic windows are retired, so there is no placeholder
    # linking to them any more. A route whose page module were missing shows
    # the error card (Try again / Copy details) and never calls the 1.x entry
    # point it used to link to.
    from ui import shell as shell_mod
    monkeypatch.setattr(shell_mod, "_import_page_module", lambda r: None)
    assert not hasattr(shell_mod, "PlaceholderPage") and not hasattr(shell_mod, "CLASSIC")
    app = make_stub_app()
    win = shell_mod.MainWindow(app)
    try:
        page = win.open(route)
        assert isinstance(page, shell_mod.ErrorPage)
        assert "PageMissing" in page.details and route in page.details
        assert not hasattr(page, "classic_button")
        assert {b.text() for b in page.findChildren(QAbstractButton)} == {"Try again", "Copy details"}
        assert (method,) not in app.calls
    finally:
        win.deleteLater()


@pytest.mark.parametrize("route", ["home", "dictation", "smart_actions", "meetings", "live_assistant",
                                   "audio_to_text", "dictionary", "settings", "account"])
def test_real_page_renders_for_every_route(shell, route):
    # Wave 3: ui/pages/<route>.py exists, so the route builds its own page
    # (its create()), not the placeholder and not the error card.
    from ui import shell as shell_mod
    mod = shell_mod._import_page_module(route)
    assert mod is not None and callable(getattr(mod, "create", None)), route
    assert shell_mod.page_factory(route) is mod.create
    for tier in ("free", "pro", "guest"):
        win, _ = shell(tier)
        win.show()
        page = win.open(route)
        QApplication.processEvents()
        assert isinstance(page, QWidget)
        assert not isinstance(page, shell_mod.ErrorPage), (tier, route)
        assert not hasattr(page, "classic_button"), (tier, route)


def test_free_pro_guest_sidebar(shell):
    from ui.components import FreeBadge, GuestBadge, ProTag
    free, _ = shell("free")
    assert free.sidebar.tier() == "free"
    assert free.sidebar.upgrade_button is not None
    assert free.sidebar.upgrade_button.text() == "Upgrade to Pro"
    assert isinstance(free.sidebar.account_row.badge, FreeBadge)
    assert free.sidebar.account_row.name_label.text() == "Aram"

    pro, _ = shell("pro")
    assert pro.sidebar.tier() == "pro"
    assert pro.sidebar.upgrade_button is None    # calm Pro: no upgrade anywhere
    assert isinstance(pro.sidebar.account_row.badge, ProTag)

    guest, _ = shell("guest")
    assert guest.sidebar.tier() == "guest"
    assert isinstance(guest.sidebar.account_row.badge, GuestBadge)
    assert guest.sidebar.account_row.name_label.text() == "Guest"

    # No PRO tags in the sidebar for anyone (a Free user who uses a Pro
    # feature gets the Pro window there).
    for win in (free, pro, guest):
        assert [r for r, it in win.sidebar.items.items() if it.pro_tag is not None] == []
        assert "Pro" not in win.sidebar.items["smart_actions"].accessibleName()


def test_refresh_account_follows_the_plan(shell):
    win, app = shell("free")
    home = win.open("home")
    app.set_tier("pro")
    win.refresh_account()
    assert win.sidebar.tier() == "pro" and win.sidebar.upgrade_button is None
    app.set_tier("guest")
    win.refresh_account()
    assert win.sidebar.tier() == "guest"
    assert home.header.title_label.text() == "Welcome"


def test_sidebar_signals(shell):
    win, app = shell("free")
    win.show()
    win.sidebar.items["dictionary"].click()
    assert win.current_route() == "dictionary"
    win.sidebar.settings_item.click()
    assert win.current_route() == "settings"
    win.sidebar.upgrade_button.click()
    assert ("_pro_upsell", None) in app.calls
    win.sidebar.account_row.click()
    assert win.current_route() == "account"


def test_pro_account_row_is_highlighted_on_account(shell):
    win, _ = shell("pro")
    win.open("account")
    assert win.sidebar.account_row.property("active") is True
    win.open("home")
    assert win.sidebar.account_row.property("active") is False


def test_account_highlight_follows_a_plan_change(shell):
    # Only the Pro row lights up on Account & plan; a Free card never does.
    win, app = shell("pro")
    win.open("account")
    assert win.sidebar.account_row.property("active") is True
    app.set_tier("free")
    win.refresh_account()
    assert win.sidebar.account_row.property("active") is False
    app.set_tier("pro")
    win.refresh_account()
    assert win.sidebar.account_row.property("active") is True


def test_sidebar_and_header_geometry_match_the_mockup(shell):
    """Home.dc.html: nav padding 16/12, brand 44 + 10 margin, items 38 tall with a
    3 px gap, the plan card 12 px above Settings, header 28/20 around a 34 px title."""
    win, _ = shell("free")
    win.resize(1280, 820)
    win.show()
    win.open("home")
    QApplication.processEvents()
    sb = win.sidebar
    top = [sb.items[r].geometry().top() for r in ("home", "dictation", "smart_actions")]
    assert top == [73, 114, 155]
    assert all(sb.items[r].height() == 38 for r in sb.items)
    settings = sb.settings_item.geometry()
    assert settings.bottom() + 1 == sb.height() - 16
    assert settings.top() - (sb._acct_slot.geometry().bottom() + 1) == 12
    # The Free card: avatar and badge on its 10 px padding line, Upgrade full width.
    card = sb.upgrade_button.parentWidget()
    row = sb.account_row
    avatar_x = row.avatar.mapTo(card, row.avatar.rect().topLeft()).x()
    assert avatar_x == 1 + 10
    up = sb.upgrade_button.geometry()
    assert (up.left(), card.width() - 1 - up.right()) == (11, 11)
    assert up.height() == 30
    header = win.page("home").header
    assert header.height() == 28 + 34 + 20
    assert header.title_label.height() == 34


def test_broken_page_shows_error_card_and_recovers(shell):
    from ui import shell as shell_mod
    from ui.components import Card

    attempts = []

    def factory(main_app):
        attempts.append(1)
        if len(attempts) == 1:
            raise RuntimeError("kaboom")
        w = Card()
        w.marker = "ok"
        return w

    shell_mod.register_page("dictionary", factory)
    try:
        win, _ = shell()
        page = win.open("dictionary")
        assert isinstance(page, shell_mod.ErrorPage)
        assert "kaboom" in page.details
        page.copy_button.click()
        assert "kaboom" in QGuiApplication.clipboard().text()
        page.retry_button.click()
        fixed = win.page("dictionary")
        assert getattr(fixed, "marker", None) == "ok"
        assert win.current_route() == "dictionary"
        # Other pages are untouched.
        assert not isinstance(win.open("home"), shell_mod.ErrorPage)
    finally:
        shell_mod.unregister_page("dictionary")


def test_factory_must_return_a_widget(shell):
    from ui import shell as shell_mod
    shell_mod.register_page("dictionary", lambda app: "not a widget")
    try:
        win, _ = shell()
        assert isinstance(win.open("dictionary"), shell_mod.ErrorPage)
    finally:
        shell_mod.unregister_page("dictionary")


def test_page_hooks(shell):
    from ui import shell as shell_mod
    from ui.components import Card
    seen = []

    class Page(Card):
        def on_open(self, **opts):
            seen.append(("open", opts))

        def on_setting_changed(self, key):
            seen.append(("setting", key))

        def refresh_account(self):
            seen.append(("account",))

    shell_mod.register_page("dictation", lambda app: Page())
    try:
        win, _ = shell()
        win.open("dictation", focus="hotkey")
        win.on_setting_changed("hotkey")
        win.refresh_account()
        assert seen == [("open", {"focus": "hotkey"}), ("setting", "hotkey"), ("account",)]
        # A hook that raises never breaks the window.
        Page.on_setting_changed = lambda self, key: 1 / 0
        win.on_setting_changed("language")
    finally:
        shell_mod.unregister_page("dictation")


def test_missing_page_module_means_placeholder(qapp, monkeypatch):
    from ui import shell as shell_mod
    # Wave 4: no placeholder any more - a route without its module gets a
    # factory that raises PageMissing, which the window shows as its error card.
    monkeypatch.setattr(shell_mod, "_import_page_module", lambda r: None)
    with pytest.raises(shell_mod.PageMissing):
        shell_mod.page_factory("dictionary")(make_stub_app())
    win = shell_mod.MainWindow(make_stub_app())
    try:
        assert isinstance(win.open("dictionary"), shell_mod.ErrorPage)
    finally:
        win.deleteLater()


def test_close_only_hides(shell):
    win, _ = shell()
    win.show()
    QApplication.processEvents()
    assert win.isVisible()
    win.close()
    QApplication.processEvents()
    assert not win.isVisible()
    win.open("settings")                         # still alive and usable
    assert win.current_route() == "settings"


def test_quitting_closes_it_so_qt_does_not_cancel_the_quit(shell):
    # Qt's Quit closes every window and stops at the first that refuses; a
    # window left up (e.g. the Live Assistant card) then cancels the quit.
    from PySide6.QtGui import QCloseEvent
    win, app = shell()
    win.show()
    QApplication.processEvents()
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert not ev.isAccepted() and not win.isVisible()      # a normal close: hidden, app keeps running
    win.show()
    app._quitting = True                                    # AppController.quit_app sets it first
    ev = QCloseEvent()
    win.closeEvent(ev)
    assert ev.isAccepted()


def test_every_button_has_an_accessible_name(shell):
    from ui.shell import ROUTES
    win, _ = shell("free")
    for route in ROUTES:
        win.open(route)
    missing = []
    for b in win.findChildren(QAbstractButton):
        if not (b.text() or "").strip() and not (b.accessibleName() or "").strip():
            missing.append("%s(%s)" % (type(b).__name__, b.objectName()))
    assert not missing, missing
    # The logo, the only image in the sidebar, is named too.
    assert win.sidebar.logo.accessibleName() == "Gusan"


def test_keyboard_reaches_the_sidebar(shell):
    win, _ = shell("free")
    win.show()
    win.activateWindow()
    QApplication.processEvents()
    item = win.sidebar.items["dictation"]
    item.setFocus(Qt.TabFocusReason)
    QApplication.processEvents()
    assert item.property("focusVisible") is True
    QTest.keyClick(item, Qt.Key_Space)
    assert win.current_route() == "dictation"
