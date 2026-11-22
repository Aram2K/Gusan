"""No "&" is eaten as a shortcut-key mark: Qt reads a lone "&" in a button's, a check box's, a menu row's, a group
box's or a tab's words as a mnemonic - it hides the "&" and underlines the next letter (the Dictation page's link
once read "Speech _AI settings"). Words with an "&" write it "&&" there. Labels are not affected (a QLabel only
does that with a buddy, which Gusan never sets).

Every main page (every Settings section too), for a Pro, a Free and a guest account, and the windows with buttons
of their own (the Pro window, the sign-in sheet, every welcome step) are built through the app's own code and
every piece of text that Qt would read as a mnemonic is checked.
"""
import pytest
from PySide6.QtWidgets import QAbstractButton, QApplication, QGroupBox, QMenu, QTabBar, QWidget

from tests_qt.conftest import make_stub_app

ROUTES = ("home", "dictation", "smart_actions", "meetings", "live_assistant", "audio_to_text", "invite", "account")


def lone_amp(text):
    """True when ``text`` has an "&" that Qt would take as a shortcut-key mark."""
    return "&" in str(text or "").replace("&&", "")


def offenders(root):
    out = []
    for w in [root] + root.findChildren(QWidget):
        try:
            # (Qt's own text: a sidebar entry overrides text() with the words of its label, which shows an "&")
            if isinstance(w, QAbstractButton) and lone_amp(QAbstractButton.text(w)):
                out.append("%s %r" % (type(w).__name__, QAbstractButton.text(w)))
            elif isinstance(w, QGroupBox) and lone_amp(w.title()):
                out.append("group box %r" % w.title())
            elif isinstance(w, QTabBar):
                out += ["tab %r" % w.tabText(i) for i in range(w.count()) if lone_amp(w.tabText(i))]
            if isinstance(w, QMenu) and lone_amp(w.title()):
                out.append("menu %r" % w.title())
            for a in QWidget.actions(w):
                if not a.isSeparator() and lone_amp(a.text()):
                    out.append("action %r" % a.text())
        except RuntimeError:
            continue
    return out


def _spin(n=4):
    for _ in range(n):
        QApplication.processEvents()


def test_the_detector():
    assert lone_amp("Speech & AI settings")
    assert not lone_amp("Speech && AI settings")
    assert not lone_amp("Q&&A notes") and not lone_amp("Plain words")


@pytest.mark.parametrize("tier", ["pro", "free", "guest"])
def test_no_page_eats_an_ampersand(qapp, tier, monkeypatch):
    import ui.winfit
    from ui import shell
    from ui.pages import settings as S
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    monkeypatch.setattr(ui.winfit, "fit_on_screen", lambda *a, **k: None)
    win = shell.MainWindow(make_stub_app(tier))
    win.resize(1280, 860)
    win.show()
    try:
        bad = []
        for route in ROUTES:
            win.open(route)
            _spin()
            bad += ["%s: %s" % (route, o) for o in offenders(win)]
        for sec in S.SECTION_IDS:
            page = win.open("settings", section=sec)
            page.ensure_built()
            _spin()
            bad += ["settings/%s: %s" % (sec, o) for o in offenders(win)]
        assert not sorted(set(bad)), "a lone & that Qt turns into a shortcut-key mark:\n  " + "\n  ".join(sorted(set(bad)))
    finally:
        win.hide()
        win.deleteLater()
        _spin()


def test_no_window_eats_an_ampersand(qapp):
    from tests_qt.test_page_onboarding import app_for, sync_run
    from ui import onboarding_v2 as onb
    from ui.auth_sheet import AuthSheet
    from ui.pro_dialog import ProDialog
    made = []
    try:
        app = make_stub_app("free")
        app.save_config = lambda: None
        made.append(ProDialog(main_app=app, feature="Meeting recording"))
        for mode in ("signin", "signup"):
            made.append(AuthSheet(app_for("guest"), mode=mode, run=sync_run))
        wiz = onb.OnboardingWizard(app_for("guest"), run=sync_run, animate=False)
        made.append(wiz)
        bad = []
        for w in made:
            w.show()
            _spin()
            bad += offenders(w)
        for name in onb.PAGES:
            wiz.go(name)
            _spin()
            bad += ["welcome/%s: %s" % (name, o) for o in offenders(wiz)]
        assert not sorted(set(bad)), "a lone & that Qt turns into a shortcut-key mark:\n  " + "\n  ".join(sorted(set(bad)))
    finally:
        for w in made:
            w.hide()
            w.deleteLater()
        _spin()
