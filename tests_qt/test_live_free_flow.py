"""Live Assistant for everyone: Open works for Free and guests (the real floating
card, never the Pro window), the PRO pill sits beside the page title and beside the
card's title (hidden for Pro, it follows a plan change), and Start is where Free
meets the Pro window. Nothing else on the card is a dead click for Free."""
import time
from types import SimpleNamespace
from unittest import mock

import pytest
from PySide6.QtCore import QObject, QPoint, Signal
from PySide6.QtWidgets import QApplication, QLabel, QWidget

from tests_qt.conftest import make_stub_app
# The Meetings page's fixtures (a shell with a stub app, a throwaway library, a fake recorder).
from tests_qt.test_page_meetings import FakeSession, _inline_bg, _open, meetings_root, shell  # noqa: F401


# ── the page ─────────────────────────────────────────────────────────────────
@pytest.fixture
def la(qapp, monkeypatch):
    import live_context
    from ui.pages import live_assistant
    real = live_context.hide_status
    monkeypatch.setattr(live_context, "hide_status",
                        lambda wanted, **kw: real(wanted, **dict(dict(supported=True, remote=False, mac=False), **kw)))
    monkeypatch.setattr(live_assistant, "load_sessions", lambda: [])
    return live_assistant


@pytest.fixture
def build_page(la):
    made = []

    def _build(tier="free", with_show=True):
        app = make_stub_app(tier)
        app.live_assist = None
        app.show_main = lambda *a, **k: app.calls.append(("show_main",) + a)
        if with_show:
            app.show_live_assist = lambda: app.calls.append(("show_live_assist",))
        host = QWidget()
        from ui import theme
        theme.apply(host)
        host.resize(1048, 1000)
        page = la.create(app)
        page.setParent(host)
        page.resize(1048, 1000)
        host.show()
        page.on_open()
        QApplication.processEvents()
        made.append(host)
        return page, app

    yield _build
    for h in made:
        h.hide()
        h.deleteLater()
    QApplication.processEvents()


def _calls(app, name):
    return [c for c in app.calls if c[0] == name]


@pytest.mark.parametrize("tier", ["free", "guest", "pro"])
def test_open_shows_the_card_for_every_plan(build_page, tier):
    page, app = build_page(tier)
    page.open_button.click()
    assert _calls(app, "show_live_assist") == [("show_live_assist",)]
    assert not _calls(app, "_pro_upsell")                   # no Pro window on Open, for anyone


@pytest.mark.parametrize("tier", ["free", "guest"])
def test_open_falls_back_to_the_existing_card_and_the_toggle(build_page, tier):
    page, app = build_page(tier, with_show=False)
    page.open_button.click()                                # no card yet: the controller builds it
    assert _calls(app, "toggle_live_assist") and not _calls(app, "_pro_upsell")
    opened = []
    app.live_assist = SimpleNamespace(open_card=lambda: opened.append(1))
    app.toggle_live_assist = lambda: app.calls.append(("toggle_again",))
    page._attach_card = lambda: None                        # a stand-in card is no QObject
    page.open_button.click()
    assert opened == [1] and not _calls(app, "toggle_again") and not _calls(app, "_pro_upsell")


@pytest.mark.parametrize("tier", ["free", "guest", "pro"])
def test_the_pro_pill_sits_beside_the_title_for_every_plan(build_page, tier):
    from ui.components import ProTag
    page, _ = build_page(tier)
    pill, title, chip = page.pro_tag, page.header.title_label, page.status_chip
    assert isinstance(pill, ProTag) and pill.text() == "PRO" and pill.isVisibleTo(page)
    assert page.header.isAncestorOf(pill)
    t = title.mapTo(page, QPoint(0, 0))
    p = pill.mapTo(page, QPoint(0, 0))
    c = chip.mapTo(page, QPoint(0, 0))
    assert t.x() + title.width() <= p.x() and p.x() + pill.width() <= c.x()     # title, PRO, then the chip
    assert abs((p.y() + pill.height() / 2) - (t.y() + title.height() / 2)) <= 6   # on the title's line
    assert pill.accessibleName() == "Pro feature" and pill.toolTip() == "Pro feature"
    assert page.header.accessibleName() == "Live Assistant, Pro feature"
    assert page.header.title_label.accessibleName() == "Live Assistant"


@pytest.mark.parametrize("tier", ["free", "pro"])
def test_no_pill_beside_the_open_button(build_page, tier):
    from ui.components import ProTag
    page, _ = build_page(tier)
    hero = page.actions.parentWidget()
    assert not hero.findChildren(ProTag)
    assert not hasattr(page, "open_pro_tag")
    assert page.open_button.accessibleDescription() == ""   # opening is not a Pro action any more
    # button, "or", then the keycaps - left to right on one line.
    b = page.open_button.mapTo(page, QPoint(0, 0))
    o = page.or_label.mapTo(page, QPoint(0, 0))
    k = page.hero_keys.mapTo(page, QPoint(0, 0))
    assert page.actions.one_line()
    assert b.x() + page.open_button.width() <= o.x() < o.x() + page.or_label.width() <= k.x()
    page.resize(700, 1000)                                  # a narrow column: the keys drop under the button
    QApplication.processEvents()
    assert not page.actions.one_line()
    k = page.hero_keys.mapTo(page, QPoint(0, 0))
    b = page.open_button.mapTo(page, QPoint(0, 0))
    assert k.y() >= b.y() + page.open_button.height() - 2
    page.resize(1048, 1000)
    QApplication.processEvents()
    assert page.actions.one_line()


def test_the_fair_use_line_is_gone(build_page, la):
    page, _ = build_page("free")
    assert not hasattr(page, "fair_use") and not hasattr(la, "FAIR_USE")
    texts = [w.text() for w in page.findChildren(QLabel)]
    assert not [t for t in texts if "exams" in t or "interviews" in t or "help isn" in t]
    assert "fair" not in (la.__doc__ or "").lower().split("integration notes")[0]


@pytest.mark.parametrize("tier", ["free", "guest"])
def test_the_local_writer_line_is_not_cut_off(build_page, tier):
    # What Free sees first: "This computer / Qwen 1.5B · not downloaded" beside its Change link.
    page, _ = build_page(tier)
    opt = page.local_option
    opt.set_choice("laptop", "This computer", "Qwen 1.5B · not downloaded")
    QApplication.processEvents()
    ml = opt.model_label
    assert ml.text() == "Qwen 1.5B · not downloaded"
    assert ml.width() >= ml.sizeHint().width(), (ml.width(), ml.sizeHint().width())
    assert page.local_change.isVisibleTo(page) and page.local_change.x() >= ml.x() + ml.width()


def test_the_sessions_card_is_the_last_thing_in_the_column(build_page):
    page, _ = build_page("free")
    sessions = page.sessions_card.mapTo(page, QPoint(0, 0)).y() + page.sessions_card.height()
    below = [w for w in page.findChildren(QLabel)
             if w.isVisibleTo(page) and w.mapTo(page, QPoint(0, 0)).y() >= sessions
             and w.mapTo(page, QPoint(0, 0)).x() < page.rail.x()]
    assert not below, [w.text() for w in below]


# ── the floating card ────────────────────────────────────────────────────────
class _App(QObject):
    """The controller bits the card uses, with a plan that can change."""
    sig_setting_changed = Signal(str)
    sig_auth_changed = Signal()

    def __init__(self, pro=False, **cfg):
        super().__init__()
        self.pro = pro
        self.cfg = {"live_assist_screen_auto": False}
        self.cfg.update(cfg)
        self.upsells = []
        self.tracked = []

    def is_pro(self):
        return self.pro

    def schedule_config_save(self):
        pass

    def save_config(self):
        pass

    def track(self, event, props=None):
        self.tracked.append(event)

    def _pro_upsell(self, feature=None):
        self.upsells.append(feature)


@pytest.fixture
def glass_ok():
    from ui import glass
    with mock.patch.object(glass, "capture_exclusion_supported", return_value=True), \
            mock.patch.object(glass, "is_remote_session", return_value=False), \
            mock.patch.object(glass, "exclude_from_capture", return_value=True), \
            mock.patch.object(glass, "is_excluded_from_capture", return_value=True), \
            mock.patch.object(glass, "macos_26_or_later", return_value=False):
        yield glass


@pytest.fixture
def card(qapp, glass_ok):
    from ui import live_assist
    made = []

    def build(pro=False, **cfg):
        app = _App(pro, **cfg)
        c = live_assist.LiveAssistOverlay(main_app=app)
        made.append(c)
        return c, app

    yield build
    for c in made:
        c.hide()
        c.deleteLater()
    QApplication.processEvents()


def _idle_session(app, started):
    app.meeting_session = SimpleNamespace(state="idle", start=lambda **kw: started.append(kw) or (True, "", ""))


def test_the_card_opens_for_free_without_the_pro_window(card):
    c, app = card(pro=False)
    c.open_card()
    assert c.isVisible()
    assert app.upsells == []                                # looking around asks for nothing
    c.toggle()                                              # the hotkey / tray path hides it again...
    assert not c.isVisible()
    c.toggle()                                              # ...and brings it back
    assert c.isVisible() and app.upsells == []


def test_free_sees_the_pro_pill_and_pro_does_not(card):
    free, _ = card(pro=False)
    free.open_card()
    pro, _ = card(pro=True)
    pro.open_card()
    assert free.lbl_pro.text() == "PRO" and free.lbl_pro.isVisibleTo(free) and free.lbl_pro.isVisible()
    assert not pro.lbl_pro.isVisibleTo(pro)
    assert free.lbl_pro.accessibleName() == "Pro feature" and "Pro" in free.lbl_pro.toolTip()


def test_the_pill_is_beside_the_title_and_start_has_none_of_its_own(card):
    c, _ = card(pro=False)
    c.open_card()
    QApplication.processEvents()
    title = c.lbl_title.mapTo(c, QPoint(0, 0))
    pill = c.lbl_pro.mapTo(c, QPoint(0, 0))
    start = c.btn_start.mapTo(c, QPoint(0, 0))
    assert title.x() + c.lbl_title.width() <= pill.x() and pill.x() + c.lbl_pro.width() <= start.x()
    assert abs((pill.y() + c.lbl_pro.height() / 2) - (title.y() + c.lbl_title.height() / 2)) <= 6
    assert c.btn_start.icon().isNull()                      # one PRO mark on the card, not two


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_the_pill_is_legible_on_both_glass_looks(card, theme):
    c, _ = card(pro=False, live_assist_theme=theme)
    c.open_card()
    QApplication.processEvents()
    img = c.lbl_pro.grab().toImage()
    purple = white = 0
    for x in range(img.width()):
        for y in range(img.height()):
            px = img.pixelColor(x, y)
            if px.alpha() < 200:
                continue
            if px.red() > 90 and px.blue() > 160 and px.green() < 110:
                purple += 1
            elif px.red() > 225 and px.green() > 225 and px.blue() > 225:
                white += 1
    assert purple > 0.45 * img.width() * img.height()       # ProTag's purple glass
    assert white >= 12                                      # the white letters on it
    assert c.lbl_pro.height() == 18 and c.lbl_pro.width() >= 28


def test_the_pill_follows_the_plan(card):
    c, app = card(pro=False)
    c.open_card()
    assert c.lbl_pro.isVisibleTo(c)
    app.pro = True
    app.sig_auth_changed.emit()                             # an upgrade while the card is open
    QApplication.processEvents()
    assert not c.lbl_pro.isVisibleTo(c)
    app.pro = False
    app.sig_auth_changed.emit()                             # a sign-out / a lapsed plan
    QApplication.processEvents()
    assert c.lbl_pro.isVisibleTo(c)


def test_the_pill_is_refreshed_on_every_open(card):
    c, app = card(pro=False)
    c.open_card()
    c.hide_overlay()
    app.pro = True                                          # the signal was missed while it was hidden
    c.open_card()
    assert not c.lbl_pro.isVisibleTo(c)
    c.hide_overlay()
    app.pro = False
    c.toggle()
    assert c.lbl_pro.isVisibleTo(c)


def test_a_plan_change_from_a_worker_thread_reaches_the_card(card):
    import threading
    c, app = card(pro=False)
    c.open_card()
    app.pro = True
    t = threading.Thread(target=app.sig_auth_changed.emit)  # AuthManager reports from a worker
    t.start()
    t.join()
    end = time.monotonic() + 3
    while c.lbl_pro.isVisibleTo(c) and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    assert not c.lbl_pro.isVisibleTo(c)


def test_a_controller_that_cannot_say_its_plan_shows_no_pill(card):
    from ui import live_assist
    app = SimpleNamespace(cfg={})
    c = live_assist.LiveAssistOverlay(main_app=app)
    try:
        c.open_card()
        assert not c.lbl_pro.isVisibleTo(c)
        assert c._pro_blocks() is False                     # and nothing blocks Start
    finally:
        c.hide()
        c.deleteLater()


def test_start_is_where_free_meets_the_pro_window(card):
    c, app = card(pro=False)
    started = []
    _idle_session(app, started)
    c.open_card()
    assert app.upsells == []
    c.btn_start.click()
    assert app.upsells == ["Live Assistant"] and started == []
    c.btn_start.click()
    assert app.upsells == ["Live Assistant", "Live Assistant"]      # every press, never a dead click


def test_start_for_free_shows_the_pro_window_even_without_a_recorder(card):
    c, app = card(pro=False)
    app.meeting_session = None
    c.btn_start.click()
    assert app.upsells == ["Live Assistant"]
    assert c.lbl_status.text() == ""                        # not "recorder isn't available"


def test_start_for_pro_goes_ahead(card):
    c, app = card(pro=True, meeting_consent_ack=True)
    started = []
    _idle_session(app, started)
    app.recorder = SimpleNamespace(fast_live_chunks=False)
    c.open_card()
    with mock.patch("threading.Thread"):
        c.btn_start.click()
    assert app.upsells == [] and len(started) == 1 and started[0]["source"] == "live_assist"


# ── nothing else is a dead click for Free ────────────────────────────────────
NO_ENGINE = "Answers need Pro, or your own key in Settings › Speech & AI."


@pytest.fixture
def no_local_model(monkeypatch):
    import actions
    monkeypatch.setattr(actions, "_first_downloaded_local_model", lambda: None)


def _resolves(app, engine, cfg=None):
    app._resolve_action_engine = lambda: (engine, dict(app.cfg, **(cfg or {})))


def test_the_no_engine_line_is_short_and_plain(card):
    from ui import live_assist
    assert live_assist.NEEDS_PRO_ANSWERS == NO_ENGINE


def test_free_without_a_key_or_model_gets_a_line_not_a_dead_click(card, no_local_model):
    import actions
    c, app = card(pro=False)
    _resolves(app, actions.RULE_BASED_ID)
    c.open_card()
    with mock.patch("ui.live_assist.threading.Thread") as thread, \
            mock.patch("ui.live_assist.capture_screen_b64") as grab:
        gen = c._gen
        for button in c.quick_buttons:                      # Answer, Follow-ups, Solve screen
            c.txt_suggestion.setPlainText("")
            button.click()
            assert c.txt_suggestion.toPlainText() == NO_ENGINE, button.text()
        c.txt_suggestion.setPlainText("")
        c.btn_suggest.click()                               # the send button with nothing typed
        assert c.txt_suggestion.toPlainText() == NO_ENGINE
        c.input_ask.setText("What is a mutex?")
        c.txt_suggestion.setPlainText("")
        c.btn_suggest.click()                               # ...and with a question typed
        assert c.txt_suggestion.toPlainText() == NO_ENGINE
        assert c.input_ask.text() == "What is a mutex?"     # the question stays, nothing "went out"
        c.suggest("", auto=True)
        assert c.txt_suggestion.toPlainText() == NO_ENGINE
        assert not thread.called                            # nothing was sent anywhere
        assert not grab.called                              # and no screen was captured for it
        assert c._gen == gen
    assert app.upsells == []                                # the Pro window is Start's job alone


def test_guests_get_the_same_line(card, no_local_model):
    import actions
    c, app = card(pro=False)                                # a guest is simply "not Pro" to the card
    _resolves(app, actions.RULE_BASED_ID)
    with mock.patch("ui.live_assist.threading.Thread") as thread:
        c.suggest("Hello?")
    assert c.txt_suggestion.toPlainText() == NO_ENGINE and not thread.called


def test_free_with_their_own_key_still_asks_as_before(card, no_local_model):
    import actions
    c, app = card(pro=False)
    _resolves(app, actions.API_CEREBRAS_ID, {"action_api_key": "csk-test"})
    with mock.patch("ui.live_assist.threading.Thread") as thread:
        c.suggest("What is a mutex?")
    assert thread.called and c.txt_suggestion.toPlainText() == "Thinking…"
    assert c._gen == 1


def test_free_with_a_downloaded_local_model_still_asks_as_before(card, monkeypatch):
    import actions
    monkeypatch.setattr(actions, "_first_downloaded_local_model", lambda: "qwen_tiny")
    c, app = card(pro=False)
    _resolves(app, actions.RULE_BASED_ID)
    with mock.patch("ui.live_assist.threading.Thread") as thread:
        c.suggest("What is a mutex?")
    assert thread.called and c.txt_suggestion.toPlainText() == "Thinking…"


def test_privacy_mode_without_a_local_model_names_the_download(card, no_local_model):
    import actions
    c, app = card(pro=False, privacy_mode=True)
    _resolves(app, actions.RULE_BASED_ID)
    with mock.patch("ui.live_assist.threading.Thread") as thread:
        c.suggest("Hello?")
    assert c.txt_suggestion.toPlainText() == actions.LOCAL_ANSWERS_MESSAGE and not thread.called


def test_pro_is_never_stopped_by_the_free_check(card):
    c, app = card(pro=True)
    seen = []
    app._resolve_action_engine = lambda: seen.append(1) or ("managed", {})
    with mock.patch("ui.live_assist.threading.Thread") as thread:
        c.suggest("What is a mutex?")
    assert thread.called and seen == []                     # not even asked: Pro goes straight out
    assert c.txt_suggestion.toPlainText() == "Thinking…"


def test_a_failing_engine_check_never_blocks(card):
    c, app = card(pro=False)

    def boom():
        raise RuntimeError("no engine table")
    app._resolve_action_engine = boom
    with mock.patch("ui.live_assist.threading.Thread") as thread:
        c.suggest("What is a mutex?")
    assert thread.called                                    # the old path decides, as before


# ── every other way to open it ───────────────────────────────────────────────
def test_the_hotkey_and_a_second_launch_open_it_without_asking_for_pro():
    import main
    seen = []
    card = SimpleNamespace(toggle=lambda: seen.append("toggle"), open_card=lambda: seen.append("open"))
    fake = SimpleNamespace(_ensure_live_assist=lambda: card, is_pro=lambda: False,
                           _pro_upsell=lambda *a, **k: seen.append("upsell"),
                           show_tray_hint=lambda *a, **k: seen.append("hint"))
    main.AppController.toggle_live_assist(fake)            # Ctrl+Alt+A (sig_assist_hotkey) and the tray row
    main.AppController.show_live_assist(fake)              # the page's Open, the Meetings live view
    ctl = SimpleNamespace(toggle_live_assist=lambda: seen.append("toggle"))
    main.dispatch_ipc_action(ctl, "live_prompter")         # a second launch of the app
    assert seen == ["toggle", "open", "toggle"]


def test_the_meetings_live_view_opens_the_card_for_free_too(shell, meetings_root):
    session = FakeSession("recording")
    win, app = shell("free", meeting_session=session)
    app.show_live_assist = lambda: app.calls.append(("show_live_assist",))
    page = _open(win)
    page.live_view.assist_btn.click()
    assert ("show_live_assist",) in app.calls
    assert ("_pro_upsell", "Live Assistance") not in app.calls and not _calls(app, "_pro_upsell")
