"""Offscreen Qt for the Gusan 2.0 window tests.

Set up before anything imports PySide6 or storage:
- QT_QPA_PLATFORM=offscreen (no window appears), and on Windows
  QT_QPA_FONTDIR=%WINDIR%\\Fonts (offscreen Qt finds no fonts without it, so
  text would have no width);
- a throwaway data folder and no keyring, so nothing touches the real
  profile (storage.py reads TRANSCRIBE_APP_DATA_DIR / TRANSCRIBE_DISABLE_KEYRING /
  TRANSCRIBE_SKIP_MIGRATION).
One QApplication serves the whole session.
"""
import os
import shutil
import sys
import tempfile
from types import SimpleNamespace

os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
_DATA = tempfile.mkdtemp(prefix="gusan-qt-test-")
os.environ["TRANSCRIBE_APP_DATA_DIR"] = _DATA
os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"
os.environ["TRANSCRIBE_NO_SELECTION"] = "1"     # never read the selection of the app in front (selection_reader)
os.environ["TRANSCRIBE_NO_WIN_HOTKEYS"] = "1"   # never take real system-wide shortcuts (win_hotkeys)
os.environ.pop("TRANSCRIBE_DEV_TIER", None)

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import pytest  # noqa: E402


def _real_qt():
    try:
        from PySide6.QtWidgets import QWidget
    except Exception:
        return False
    return QWidget.__module__.startswith("PySide6")


@pytest.fixture(scope="session")
def qapp():
    if not _real_qt():
        pytest.skip("PySide6 is replaced by stand-ins (run tests_qt on its own)")
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance() or QApplication([])
    from ui import theme
    theme.install_scheme(app, "system")       # what main() does first: the look the setting and the OS say
    theme.load_fonts()
    yield app


def os_dark_palette():
    """The palette Qt's Windows theme hands the application on a Windows set to dark mode
    (window #2b2b2b, white text, #333333 buttons). QStyleHints.setColorScheme() is a no-op on the
    offscreen platform in Qt 6.8 (colorScheme() stays Unknown, the palette does not change), so a
    dark OS is simulated by giving the application this palette BEFORE main()'s own call
    (theme.install_scheme) - which then reads the OS as dark from it."""
    from PySide6.QtGui import QColor, QPalette
    pal = QPalette()
    for group in (QPalette.Active, QPalette.Inactive, QPalette.Disabled):
        for role, col in ((QPalette.Window, "#2b2b2b"), (QPalette.WindowText, "#ffffff"),
                          (QPalette.Base, "#1f1f1f"), (QPalette.AlternateBase, "#2b2b2b"),
                          (QPalette.Text, "#ffffff"), (QPalette.Button, "#333333"),
                          (QPalette.ButtonText, "#ffffff"), (QPalette.BrightText, "#ffffff"),
                          (QPalette.PlaceholderText, "#a0a0a0"), (QPalette.Link, "#4cc2ff"),
                          (QPalette.ToolTipBase, "#2b2b2b"), (QPalette.ToolTipText, "#ffffff"),
                          (QPalette.Highlight, "#0078d4"), (QPalette.HighlightedText, "#ffffff")):
            pal.setColor(group, role, QColor(col))
    for role in (QPalette.WindowText, QPalette.Text, QPalette.ButtonText):
        pal.setColor(QPalette.Disabled, role, QColor("#808080"))
    return pal


def os_light_palette():
    """The palette of a Windows in light mode (what Qt's own Fusion style hands out)."""
    from PySide6.QtWidgets import QStyleFactory
    return QStyleFactory.create("fusion").standardPalette()


def set_os_scheme(qapp, name, pref="system"):
    """The OS is in ``name`` mode ("light" / "dark") and the app starts with app_theme ``pref``: the OS's
    palette goes in first, then the app's own start-up call (theme.install_scheme) - the order of main()."""
    from ui import theme
    qapp.setPalette(os_dark_palette() if name == "dark" else os_light_palette())
    theme.install_scheme(qapp, pref)


@pytest.fixture(params=["light", "dark"])
def scheme(request, qapp):
    """Runs the test twice: the OS is in light mode, then in dark mode - and the app (app_theme "system",
    the default) follows it."""
    set_os_scheme(qapp, request.param)
    yield request.param
    set_os_scheme(qapp, "light")


def make_stub_app(tier="free", name="Aram Adamyan", email="aram@aibuben.xyz"):
    """A stand-in AppController: cfg, auth, plan, telemetry, settings and the
    classic show_* entry points, each call recorded in ``.calls``."""
    calls = []
    state = SimpleNamespace(tier=tier)
    signed_in = tier != "guest"
    auth = SimpleNamespace(is_authenticated=signed_in, is_pro=(tier == "pro"),
                           user_name=name if signed_in else None,
                           user_email=email if signed_in else None)
    app = SimpleNamespace(
        cfg={"hotkey": "alt+r", "live_assist_hotkey": "ctrl+alt+a", "smart_actions_hotkey": "alt+s",
             "backend": "local", "whisper_model": "base", "language": "auto", "privacy_mode": False},
        auth=auth, calls=calls, state=state,
    )
    app.is_pro = lambda: state.tier == "pro"
    app.current_tier = lambda: state.tier
    app.track = lambda event, props=None: calls.append(("track", event, props))
    app.set_setting = lambda key, value: calls.append(("set_setting", key, value)) or True
    app._pro_upsell = lambda feature=None: calls.append(("_pro_upsell", feature))

    def recorder(method):
        return lambda *a, **k: calls.append((method,) + a)

    for m in ("show_settings", "show_history", "show_meeting", "show_file_transcribe",
              "toggle_live_assist", "show_auth_gate", "show_onboarding"):
        setattr(app, m, recorder(m))

    def set_tier(new_tier):
        state.tier = new_tier
        auth.is_authenticated = new_tier != "guest"
        auth.is_pro = new_tier == "pro"
        auth.user_name = name if new_tier != "guest" else None
        auth.user_email = email if new_tier != "guest" else None
    app.set_tier = set_tier
    return app


@pytest.fixture
def stub_app():
    return make_stub_app


# Test files whose fixtures keep one app (its windows) for the whole file: nothing of theirs is closed between tests.
KEEPS_WINDOWS_ACROSS_TESTS = ("test_wave4_controller",)


@pytest.fixture(autouse=True)
def _close_what_the_test_left_open(request):
    """Every window, card, popup and dialog a test leaves behind is hidden and deleted after it. Left alive, their
    timers, animations and repaints piled up in the one process: the later tests ran 10-30 times slower (12-37 s
    each in test_live_documents_ui, 0.7 s alone) and the CI step hit its 40-minute cap (2.1.2's first build).
    Windows that existed before the test (a file's shared app) are left as they are."""
    if not _real_qt() or any(name in request.module.__name__ for name in KEEPS_WINDOWS_ACROSS_TESTS):
        yield
        return
    from PySide6.QtWidgets import QApplication
    app = QApplication.instance()
    before = {id(w) for w in app.topLevelWidgets()} if app is not None else set()
    yield
    app = QApplication.instance()
    if app is None:
        return
    from PySide6.QtCore import QCoreApplication, QEvent
    for w in app.topLevelWidgets():
        if id(w) in before:
            continue
        try:
            w.hide()
            w.deleteLater()
        except RuntimeError:
            pass                                   # already gone
    QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
    app.processEvents()


@pytest.fixture(autouse=True)
def _never_ask_the_website_for_the_model_list(monkeypatch):
    """The Armenian model's list is a request to gusan.aibuben.xyz that a page makes when it opens.
    A test must never make it: once the list names the model (it does, since it was published) every
    later test in the same process would see the model in its lists, and their expectations break
    (only on a machine with internet, so it first showed on GitHub). A test that needs the model
    listed says so itself (tests_qt/test_hy_pickers.py)."""
    try:
        import speech_hy
    except Exception:
        return
    monkeypatch.setattr(speech_hy, "fetch_manifest", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _never_reach_the_invite_server(monkeypatch):
    """Invite friends (invites.py, ui/invite_hub.py): no test ever reaches the real invite function, and no window's
    invite hub asks for "me" by itself a few seconds after it is made - a test that gave its stand-in account a
    token (test_account_card, test_page_settings_account) otherwise sent a request from a hub that outlived it,
    into the next test's fake server. A test that wants the server installs its own fake (tests_qt/test_invite_ui.py)."""
    if not _real_qt():
        return
    try:
        import invites
        from ui import invite_hub
    except Exception:
        return

    def offline(*_a, **_k):
        raise ConnectionError("tests never reach the invite server")
    monkeypatch.setattr(invites, "_post", offline)
    monkeypatch.setattr(invite_hub, "REFRESH_AFTER_SIGN_IN_MS", 10 ** 9)


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA, ignore_errors=True)
