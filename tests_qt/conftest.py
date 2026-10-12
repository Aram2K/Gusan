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
    theme.load_fonts()
    yield app


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


def pytest_sessionfinish(session, exitstatus):
    shutil.rmtree(_DATA, ignore_errors=True)
