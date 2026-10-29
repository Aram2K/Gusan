"""No "Transcribe" left on screen (Gusan 2.0 rename guard).

Every string a person can see or hear is collected from the real widgets,
offscreen: each page of the Gusan window for Guest / Free / Pro (every
Settings section, the Meetings live and notes views), the 5-step welcome
(every step), the sign-in sheet, the Pro window, the tray menu and the
floating surfaces (the Live Assistant card, the dictation bar, the meeting
reminder). Text, tooltips, accessible names and descriptions, placeholders,
combo items and menu rows all count.

Painted-only text (the dictation bar's lines, the tray hints, notifications)
is checked at the source: every string literal of the app's modules.

Not the old name (kept on purpose, never shown as the product): identifiers
such as TranscribeApp.exe / TranscribeApp-Windows-Setup.exe, the data folder
and keyring names, and the verb starting an action ("Transcribe a file",
"Transcribe only").
"""
import ast
import os
import re
from datetime import datetime, timedelta, timezone

import pytest
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QGroupBox, QLabel, QLineEdit, QMenu,
                               QPlainTextEdit, QTabBar, QTextEdit, QWidget)

from tests_qt.conftest import make_stub_app

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OLD = re.compile(r"\bTranscribe\b")
# OpenAI's own model names (what Gusan Cloud's server answers with) end in the word: not our old name.
_MODEL_NAMES = re.compile(r"\bGPT(?:-4o(?: mini)?)? Transcribe\b")
_SENTENCE_START = (".", ":", "!", "?", "•", "\n", "(", "\"", "“", "'")


def old_names(text):
    """The text when it still names the app "Transcribe" (else None). The verb
    starting an action or a sentence ("Transcribe a file") is fine."""
    shown = text or ""
    text = _MODEL_NAMES.sub("GPT-model", shown)
    for m in OLD.finditer(text):
        before = text[:m.start()].rstrip()
        after = text[m.end():]
        verb = (not before or before.endswith(_SENTENCE_START)) and re.match(r"\s+[a-z]", after)
        if not verb:
            return shown
    return None


def test_the_rule():
    for shown in ("Welcome to Transcribe", "Transcribe Pro", "Restart Transcribe and try again.",
                  "Transcribe", "Quit Transcribe", "Transcribe Files", "Open Transcribe",
                  "GPT Transcribe, Transcribe Pro"):
        assert old_names(shown) == shown, shown
    for fine in ("Transcribe a file", "Transcribe only", "Done. Transcribe another one?",
                 "TranscribeApp-Windows-Setup.exe", "Transcribed on this computer", "Gusan", "",
                 "GPT Transcribe", "GPT-4o Transcribe", "GPT-4o mini Transcribe", "Fast · GPT Transcribe · Cloud"):
        assert old_names(fine) is None, fine


# ── collecting what is on screen ─────────────────────────────────────────────
def _menu_texts(menu, depth=0):
    out = [menu.title()]
    for a in menu.actions():
        out += [a.text(), a.toolTip(), a.iconText(), a.statusTip(), a.whatsThis()]
        sub = a.menu()
        if sub is not None and depth < 4:
            out += _menu_texts(sub, depth + 1)
    return out


def _texts(w):
    out = [w.windowTitle(), w.toolTip(), w.accessibleName(), w.accessibleDescription(),
           w.statusTip(), w.whatsThis()]
    if isinstance(w, (QLabel, QAbstractButton)):
        out.append(w.text())
    if isinstance(w, QLineEdit):
        out += [w.text(), w.placeholderText()]
    if isinstance(w, (QPlainTextEdit, QTextEdit)):
        out += [w.toPlainText(), w.placeholderText()]
    if isinstance(w, QComboBox):
        out += [w.itemText(i) for i in range(w.count())]
        out += [str(w.itemData(i, 3) or "") for i in range(w.count())]          # Qt.ToolTipRole
    if isinstance(w, QTabBar):
        out += [w.tabText(i) for i in range(w.count())] + [w.tabToolTip(i) for i in range(w.count())]
    if isinstance(w, QGroupBox):
        out.append(w.title())
    if isinstance(w, QMenu):
        out += _menu_texts(w)
    for a in QWidget.actions(w):                 # (a page may name an attribute "actions")
        out += [a.text(), a.toolTip()]
    return [t for t in out if t]


def visible_strings(root):
    out = []
    for w in [root] + root.findChildren(QWidget):
        out += _texts(w)
    return out


def assert_no_old_name(where, strings):
    bad = sorted({s for s in strings if old_names(s)})
    assert not bad, "%s still says Transcribe: %r" % (where, bad)


# ── fixtures ─────────────────────────────────────────────────────────────────
@pytest.fixture
def meetings_root(tmp_path, monkeypatch):
    import storage
    root = tmp_path / "meetings"
    root.mkdir()
    real = storage.path_for
    monkeypatch.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    return root


@pytest.fixture
def quiet(qapp, monkeypatch):
    """Nothing reaches the network, the disk scan or the screen edge."""
    import ui.winfit
    from ui.pages import home, meeting_views
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    monkeypatch.setattr(meeting_views, "SYNC_BG", True)
    monkeypatch.setattr(home, "load_sources", lambda: ([], [], []))
    made = []
    yield made
    for w in made:
        try:
            w.hide()
            w.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


# ── the Gusan window ─────────────────────────────────────────────────────────
@pytest.mark.parametrize("tier", ["guest", "free", "pro"])
def test_every_page_of_the_window(quiet, meetings_root, tier):
    from ui import shell as shell_mod
    from ui.pages import settings as settings_page
    app = make_stub_app(tier)
    win = shell_mod.MainWindow(app)
    quiet.append(win)
    win.show()
    for route in shell_mod.ROUTES:
        page = win.open(route)
        QApplication.processEvents()
        assert not isinstance(page, shell_mod.ErrorPage), route
        if route == "settings":
            for sid in settings_page.SECTION_IDS:
                page.show_section(sid)
                QApplication.processEvents()
        strings = visible_strings(page)
        assert len(strings) > 10, route                 # the collector really sees the page
        assert_no_old_name("%s (%s)" % (route, tier), strings)
    everything = visible_strings(win)
    assert "Gusan" in everything and "Account & plan" in everything
    assert_no_old_name("the window (%s)" % tier, everything)


def test_the_meetings_live_and_notes_views(quiet, meetings_root):
    from tests_qt.test_page_meetings import FakeCalendar, FakeSession, _meeting
    from ui import shell as shell_mod
    folder = _meeting(meetings_root, "20261007_110000", "Q4 roadmap",
                      notices=["The saved recording ends at about 2 h 00 min."])
    session = FakeSession("recording")
    app = make_stub_app("pro")
    app.meeting_session = session
    app.calendar = FakeCalendar(True)
    win = shell_mod.MainWindow(app)
    quiet.append(win)
    page = win.open("meetings")
    assert page.current_view() == "live"
    assert_no_old_name("Meetings live view", visible_strings(page))
    page.show_list(refresh=True)
    assert_no_old_name("Meetings list with a calendar", visible_strings(page))
    page.open_notes(str(folder))
    QApplication.processEvents()
    assert page.current_view() == "notes"
    assert_no_old_name("Meetings notes view", visible_strings(page))


def test_the_update_window_in_every_stage(quiet):
    """The in-app update window (ui/update_window.py): its title, every stage's words and buttons."""
    from ui.update_window import UpdateWindow
    w = UpdateWindow("v2.0.2")
    quiet.append(w)
    w.show()
    mb = 2 ** 20
    seen = set()
    for name, go in (("downloading", lambda: w.show_downloading()),
                     ("progress", lambda: w.show_progress(45 * mb, 100 * mb)),
                     ("checking", lambda: w.show_progress(100 * mb, 100 * mb)),
                     ("waiting", lambda: w.show_waiting()),
                     ("waiting for a typed key", lambda: w.show_waiting(can_install=True)),
                     ("installing", lambda: w.show_installing()),
                     ("installing as administrator", lambda: w.show_installing(admin=True)),
                     ("installing, nothing reopens it", lambda: w.show_installing(reopens=False)),
                     ("failed", lambda: w.show_failed("Couldn't download the update.", "Details: C:\\x.log"))):
        go()
        QApplication.processEvents()
        strings = visible_strings(w)
        assert len(strings) > 3, name
        assert_no_old_name("the update window (%s)" % name, strings)
        seen.update(strings)
    assert "Updating Gusan" in seen and "Try again" in seen


# ── the welcome, the sign-in sheet, the Pro window ───────────────────────────
@pytest.mark.parametrize("tier", ["guest", "pro"])
def test_every_step_of_the_welcome(quiet, tier):
    from tests_qt.test_page_onboarding import app_for, sync_run
    from ui import onboarding_v2 as onb
    app = app_for(tier)
    w = onb.OnboardingWizard(app, run=sync_run, animate=False)
    quiet.append(w)
    w.show()
    for name in onb.PAGES:
        w.go(name)
        QApplication.processEvents()
        assert_no_old_name("welcome step %s (%s)" % (name, tier), visible_strings(w))


@pytest.mark.parametrize("mode", ["signin", "signup"])
def test_the_sign_in_sheet(quiet, mode):
    from tests_qt.test_page_onboarding import app_for, sync_run
    from ui.auth_sheet import AuthSheet
    sheet = AuthSheet(app_for("guest"), mode=mode, run=sync_run)
    quiet.append(sheet)
    sheet.show()
    QApplication.processEvents()
    assert_no_old_name("sign-in sheet (%s)" % mode, visible_strings(sheet))


def test_the_notion_sheet(quiet, monkeypatch):
    """All five screens, their messages and the picker, with the rail's Notion rows."""
    import notion_connect
    from tests_qt.test_notion_sheet import MCP_CFG, OAUTH_CFG, PAGES, Deferred, Host
    from ui import notion_sheet as ns
    sheets = []

    def build(cfg=None, **kw):
        sheet = ns.NotionSheet(Host(cfg), run=Deferred(), **kw)
        quiet.append(sheet)
        sheet.show()
        QApplication.processEvents()
        sheets.append(sheet)
        return sheet
    intro = build()
    assert_no_old_name("Notion sheet (first screen)", visible_strings(intro))
    for sentence in notion_connect.MESSAGES.values():
        intro._show_intro(sentence)
        assert_no_old_name("Notion sheet (first screen, %r)" % sentence, visible_strings(intro))
    intro.connect_btn.click()
    assert intro.page_name() == "waiting"
    assert_no_old_name("Notion sheet (waiting)", visible_strings(intro))
    intro.flow.finishing.emit()
    assert_no_old_name("Notion sheet (setting things up)", visible_strings(intro))
    button = build(MCP_CFG, then="Send this meeting")
    assert button.page_name() == "connected"
    assert_no_old_name("Notion sheet (connected, one private page)", visible_strings(button))
    connected = build(OAUTH_CFG, then="Send this meeting")
    connected._fill_picker(PAGES)
    assert connected.page_name() == "connected"
    assert_no_old_name("Notion sheet (connected, picker)", visible_strings(connected))
    manual = build()
    manual._show_manual(notion_connect.MESSAGES["rejected"])
    assert_no_old_name("Notion sheet (own integration)", visible_strings(manual))
    share = build()
    share._on_no_page({"mode": "oauth", "token": "t", "refresh": "", "workspace": "", "dest": None, "pages": []})
    assert share.page_name() == "share"
    assert_no_old_name("Notion sheet (share a page)", visible_strings(share))
    share._on_no_page({"mode": "own", "token": "t", "refresh": "", "workspace": "", "dest": None, "pages": []})
    assert_no_old_name("Notion sheet (share a page, own)", visible_strings(share))
    assert any("Gusan" in s for s in visible_strings(connected) + visible_strings(share) + visible_strings(button))


def test_the_notion_rows_of_the_meeting_settings(quiet, meetings_root):
    from ui import shell as shell_mod
    app = make_stub_app("pro")
    app.cfg.update({"notion_api_key": "tok", "notion_auth_mode": "mcp", "notion_workspace_name": "Aram's workspace",
                    "notion_parent_page": "99999999-9999-9999-9999-999999999999", "notion_auto_send": True})
    win = shell_mod.MainWindow(app)
    quiet.append(win)
    page = win.open("meetings")
    QApplication.processEvents()
    strings = visible_strings(page.list_view.rail)
    assert "Send notes to Notion" in strings and "Send automatically" in strings
    assert_no_old_name("Meeting settings (Notion connected)", strings)
    app.cfg["notion_reconnect"] = True
    page.list_view.rail.sync_settings()
    assert_no_old_name("Meeting settings (Notion reconnect)", visible_strings(page.list_view.rail))


@pytest.mark.parametrize("feature", [None, "Live Assistant", "Meeting recording", "Smart Actions"])
def test_the_pro_window(quiet, feature):
    from ui.pro_dialog import ProDialog
    app = make_stub_app("free")
    app.save_config = lambda: None
    dlg = ProDialog(main_app=app, feature=feature)
    quiet.append(dlg)
    assert_no_old_name("Pro window (%s)" % feature, visible_strings(dlg))


# ── the tray and the floating surfaces ───────────────────────────────────────
@pytest.mark.parametrize("tier", ["guest", "free", "pro"])
def test_the_tray_menu(quiet, tier):
    from tests_qt.test_page_floating import FakeCalendar, full_app, meeting
    from ui import tray_menu
    app = full_app(tier)
    app.calendar = FakeCalendar(meeting(start_in_s=600))
    tr = tray_menu.build_tray_menu(app, QMenu())
    quiet.append(tr.menu)
    tr.refresh()
    strings = [t for _k, t in tr.visible_texts()] + _menu_texts(tr.menu) + [tr.tooltip()]
    assert_no_old_name("tray (%s)" % tier, strings)


def test_the_live_assistant_card(quiet):
    from tests_qt.test_live_card import _App
    from ui import live_assist
    card = live_assist.LiveAssistOverlay(main_app=_App({"overlay_private": True}))
    quiet.append(card)
    card.show()
    QApplication.processEvents()
    assert_no_old_name("Live Assistant card", visible_strings(card))


def test_the_dictation_bar(quiet):
    from types import SimpleNamespace
    import ui.overlay as ov
    hud = ov.Overlay(main_app=SimpleNamespace(cfg={"backend": "local"}, save_config=lambda: None))
    quiet.append(hud)
    try:
        lines = [ov._error_hint("something odd"), ov._transcription_ai_label({"backend": "local"})]
        for state in ("recording", "transcribing"):
            lines += list(hud._busy_label(state))
        hud.show_overlay(ov.TRANSCRIBING)
        hud.set_state(ov.GETTING_READY)
        hud.set_model_progress(42)
        lines += list(hud._busy_label("transcribing"))
        assert_no_old_name("dictation bar", lines + visible_strings(hud))
    finally:
        hud.timer.stop()


def test_the_meeting_reminder(quiet):
    from tests_qt.test_page_floating import NOW, full_app, meeting
    from ui.floating import meeting_reminder as mr
    for tier in ("free", "pro"):
        t = mr.MeetingReminder(full_app(tier), clock=lambda: NOW)
        quiet.append(t)
        assert t.show_meeting(meeting()) is True
        QApplication.processEvents()
        assert_no_old_name("meeting reminder (%s)" % tier, visible_strings(t))


# ── painted text, notifications and messages: the source ─────────────────────
_RETIRED = {"settings.py", "history.py", "meeting_detail.py", "meetings.py", "onboarding.py",
            "shiny_tabs.py", "mode_combo.py", "file_transcribe.py"}
# Kept identifiers that are whole string literals: (file, literal).
_KEPT = {("storage.py", "Transcribe"), ("autostart.py", "Transcribe"), ("autostart.py", "Transcribe.lnk"),
         ("gpu_accel.py", "Transcribe")}


def _app_modules():
    skip = {"venv", ".claude", ".git", "build", "dist", "__pycache__", "tests", "tests_qt", "docs",
            "installer", "assets", "supabase", "node_modules"}
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in skip]
        rel = os.path.relpath(root, ROOT).replace("\\", "/")
        for fn in files:
            if fn.endswith(".py") and not (rel == "ui" and fn in _RETIRED):
                yield os.path.join(root, fn)


def _string_literals(path):
    """(line, text) of every string literal that isn't a docstring."""
    tree = ast.parse(open(path, encoding="utf-8").read())
    docs = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", [])
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant):
                docs.add(id(body[0].value))
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            yield node.lineno, node.value
        elif isinstance(node, ast.JoinedStr):
            text = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
            yield node.lineno, text


def test_no_old_name_in_the_apps_strings():
    bad = []
    for path in _app_modules():
        name = os.path.basename(path)
        for line, text in _string_literals(path):
            if (name, text) in _KEPT:
                continue
            text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)      # stylesheet comments aren't shown
            if old_names(text):
                bad.append("%s:%d %r" % (os.path.relpath(path, ROOT), line, text[:90]))
    assert not bad, "\n".join(bad)
