"""Gusan's screens in the light and the dark look, as pictures (a tool, not a test).

    python tools/theme_sweep.py [--out C:/g201/dark_tmp/shots] [--only home,settings] [--no-live] [--timeout 120]

Renders the main pages (Home, Dictation, Smart Actions, Meetings with a meeting open - notes and
transcript -, Live Assistant, Audio to text with a transcript, every Settings section, Account) and
the windows and cards (Pro window, sign-in sheet, What's new, every welcome step, the question card,
the answer card, the meeting reminder, the update row, the Notion sheet, the Live Assistant card,
the dictation pill's previews, Invite friends - the page with friends on the way, a month ready, the claim
moment, a month on - and the invited friend's cards on Home and Account) offscreen, once per look, to PNGs under <out>/<look>/, plus one
contact sheet per look (<out>/contact_<look>.png: a grid of labelled thumbnails).

"dark_live" is the dark look reached by SWITCHING while everything is open (built light, then
theme.switch("dark")): what a person sees when they flip Settings > Appearance or their computer
turns dark. It must look like "dark".

Each screen runs in a process of its own with a time limit (--timeout, seconds): one screen that hangs
or crashes is reported and skipped, the rest are still drawn. The contact sheets are put together at
the end from the pictures on disk.
"""
import argparse
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ["QT_QPA_PLATFORM"] = "offscreen"
if sys.platform == "win32":
    os.environ.setdefault("QT_QPA_FONTDIR", os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts"))
_DATA = tempfile.mkdtemp(prefix="gusan-sweep-")
os.environ["TRANSCRIBE_APP_DATA_DIR"] = _DATA
os.environ["TRANSCRIBE_DISABLE_KEYRING"] = "1"
os.environ["TRANSCRIBE_SKIP_MIGRATION"] = "1"
os.environ["TRANSCRIBE_NO_SELECTION"] = "1"
os.environ["TRANSCRIBE_NO_WIN_HOTKEYS"] = "1"

import pytest  # noqa: E402  (MonkeyPatch, and the test helpers import it)
from PySide6.QtCore import QCoreApplication, QEvent, QPoint, QRect, Qt  # noqa: E402
from PySide6.QtGui import QColor, QFont, QImage, QPainter  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

DEFAULT_OUT = "C:/g201/dark_tmp/shots"
SCALE = 1.5


def spin(n=4):
    for _ in range(n):
        QApplication.processEvents()


def settle(w, ms=400):
    """Let a window's layout (and a short animation) finish before it is drawn."""
    end = time.monotonic() + ms / 1000.0
    last = None
    while time.monotonic() < end:
        QApplication.processEvents()
        if w.layout() is not None:
            w.layout().activate()
        size = w.size()
        if size == last:
            break
        last = size
        time.sleep(0.02)
    spin(3)


def grab(widget, path):
    """The widget as it is painted (graphics effects included), at SCALE."""
    spin(3)
    w, h = max(1, widget.width()), max(1, widget.height())
    img = QImage(int(w * SCALE), int(h * SCALE), QImage.Format_ARGB32_Premultiplied)
    img.setDevicePixelRatio(SCALE)
    from ui import theme
    img.fill(theme.qcolor("window-b") if not widget.testAttribute(Qt.WA_TranslucentBackground)
             else QColor("#7f8794"))
    widget.render(img)
    img.save(path)
    return path


# ═════════════════════════════ the screens ═════════════════════════════
class Ctx:
    """What one look's run shares: a monkeypatch, the meetings folder, the windows made."""

    def __init__(self, mp, tmp):
        self.mp = mp
        self.tmp = tmp
        self.made = []

    def keep(self, w):
        self.made.append(w)
        return w

    def close_all(self):
        for w in self.made:
            try:
                w.hide()
                w.deleteLater()
            except RuntimeError:
                pass
        self.made = []
        QCoreApplication.sendPostedEvents(None, QEvent.DeferredDelete)
        spin()


def _setup(ctx):
    import storage
    from ui.pages import meeting_views
    from ui.components import row_actions
    import ui.winfit
    root = os.path.join(ctx.tmp, "meetings")
    os.makedirs(root, exist_ok=True)
    real = storage.path_for
    ctx.mp.setattr(storage, "path_for", lambda name: root if name == "meetings" else real(name))
    ctx.mp.setattr(meeting_views, "SYNC_BG", True)
    ctx.mp.setattr(row_actions, "ANIMATE", False)
    ctx.mp.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    ctx.mp.setattr(ui.winfit, "fit_on_screen", lambda *a, **k: None)
    try:
        import speech_hy
        ctx.mp.setattr(speech_hy, "fetch_manifest", lambda *a, **k: None)
    except Exception:
        pass
    try:
        from ui.pages import meetings as page_mod
        ctx.mp.setattr(page_mod, "_ensure_consent", lambda parent, app: True)
    except Exception:
        pass
    ctx.meetings_root = root
    # a meeting with notes and a transcript
    d = os.path.join(root, "20261030_153400")
    if not os.path.isdir(d):
        os.makedirs(d)
        with open(os.path.join(d, "meta.json"), "w", encoding="utf-8") as f:
            json.dump({"title": "Weekly product sync", "attendees": "Anna, David", "duration_sec": 3300,
                       "started": "2026-10-30 15:34:00", "language": "en", "seen": True,
                       "notes_engine_label": "Gusan Cloud · Mistral Small"}, f)
        with open(os.path.join(d, "notes.md"), "w", encoding="utf-8") as f:
            f.write("## Summary\nAram and Anna agreed to ship the **sync engine** on Friday.\n\n"
                    "## Key decisions\n- Ship 2.4 on Friday.\n- Price stays at 290 euros a year.\n\n"
                    "## Action items\n- [ ] Send the release notes (Owner: Anna, Due: Thursday)\n"
                    "- [x] Book the room (Owner: You, Due: Friday)\n\n"
                    "## Open questions\n- Do we need a beta?\n\n## Discussion\n### Release 2.4\n"
                    "- The sync engine is three times faster.\n")
        with open(os.path.join(d, "transcript.txt"), "w", encoding="utf-8") as f:
            f.write("You: Shall we ship on Friday?\nAnna: Yes, the sync engine is ready.\n"
                    "You: Great. Anna, can you send the release notes?\nAnna: Sure, by Thursday.\n")
    for i in range(3):
        o = os.path.join(root, "2026092%d_110000" % (i + 1))
        if not os.path.isdir(o):
            os.makedirs(o)
            with open(os.path.join(o, "meta.json"), "w", encoding="utf-8") as f:
                json.dump({"title": ("Design review", "Customer call", "1:1 with Anna")[i], "duration_sec": 600 * (i + 1),
                           "started": "2026-09-2%d 11:00:00" % (i + 1), "seen": i != 0}, f)
            with open(os.path.join(o, "notes.md"), "w", encoding="utf-8") as f:
                f.write("## Summary\nA short meeting.")
    ctx.meeting_folder = d


def main_window(ctx, tier="pro"):
    from tests_qt.conftest import make_stub_app
    from ui import shell
    app = make_stub_app(tier)
    win = shell.MainWindow(app)
    win.resize(1280, 860)
    win.show()
    ctx.keep(win)
    spin()
    return win


def _page(route, **opts):
    def build(ctx):
        win = main_window(ctx)
        win.open(route, **opts)
        spin(6)
        return [(route if not opts else "%s_%s" % (route, "_".join(str(v) for v in opts.values())), win)]
    return build


def s_meetings(ctx):
    win = main_window(ctx)
    out = []
    win.open("meetings")
    spin(6)
    out.append(("meetings_list", win))
    return out


def s_meeting_open(ctx):
    win = main_window(ctx)
    page = win.open("meetings", folder=ctx.meeting_folder)
    spin(6)
    nv = page.notes_view
    return [("meeting_notes", win, lambda: nv.stack_index(0)),
            ("meeting_transcript", win, lambda: nv.stack_index(1)),
            ("meeting_chat", win, lambda: (nv.stack_index(0), nv.show_chat()))]


def s_audio(ctx):
    import transcript_store
    import ui.pages.transcript_view as tv
    ctx.mp.setattr(tv, "_spawn", lambda fn: fn())
    import datetime
    win = main_window(ctx)
    transcript_store.delete_all_transcripts()
    segs = [{"start": 0.0, "end": 3.5, "text": "Thanks for coming in, Mariam.", "speaker": 0},
            {"start": 4.0, "end": 8.0, "text": "Easy, thanks. Traffic was kind today.", "speaker": 1},
            {"start": 9.0, "end": 13.0, "text": "Good. Tell me about the project.", "speaker": 0}]
    res = {"path": os.path.join(ctx.tmp, "interview_mariam.m4a"), "name": "interview_mariam.m4a",
           "duration": 24 * 60 + 20, "segments": segs, "speakers": True, "timestamps": True, "model": "small",
           "language": "en", "language_setting": "auto"}
    tid = os.path.basename(str(transcript_store.save_transcript(res, created=datetime.datetime.now().astimezone())))
    page = win.open("audio_to_text")
    spin(6)
    return [("audio_list", win, None), ("audio_transcript", win, lambda: page.open_transcript(tid))]


def s_settings(ctx):
    from ui.pages import settings as S
    win = main_window(ctx)
    out = []

    def go(sec):
        page = win.open("settings", section=sec)
        page.ensure_built()
    for sec in S.SECTION_IDS:
        go(sec)
        out.append(("settings_" + sec, win, lambda sec=sec: go(sec)))
    return out


def s_update_row(ctx):
    from tests_qt.conftest import make_stub_app
    from ui.shell import MainWindow
    win = MainWindow(make_stub_app("free"))
    win.resize(1280, 820)
    ctx.keep(win)
    page = win.open("settings", section="updates")
    page.ensure_built()
    page.update_panel.set_tag("v2.0.2")
    win.show()
    spin()
    p = page.update_panel
    return [("update_" + name, page.version_row, go) for name, go in (
        ("progress", lambda: (p.show_downloading(), p.show_progress(45 * 2 ** 20, 100 * 2 ** 20))),
        ("waiting", lambda: p.show_waiting(can_install=True)),
        ("failed", lambda: p.show_failed("The installer stopped before it finished.",
                                         "Details: C:/Users/Someone/AppData/Local/Temp/gusan.log")))]


def s_pro(ctx):
    from tests_qt.conftest import make_stub_app
    from ui.pro_dialog import ProDialog
    app = make_stub_app("free")
    app.save_config = lambda: None
    try:
        from ui.styles import STYLESHEET
        app.style_content = STYLESHEET
    except Exception:
        pass
    dlg = ctx.keep(ProDialog(main_app=app, feature="Meeting recording"))
    dlg.show()
    spin(8)
    # The plan cards dim their neighbours with opacity effects, which Qt misplaces when a widget is
    # painted onto an image (tests_qt/test_dialog_visibility.py turns them off the same way).
    for effect in getattr(dlg, "dims", {}).values():
        effect.setEnabled(False)
    return [("pro_window", dlg)]


def s_auth(ctx):
    from tests_qt.test_page_onboarding import app_for, sync_run
    from ui.auth_sheet import AuthSheet
    out = []
    for mode in ("signin", "signup"):
        sh = ctx.keep(AuthSheet(app_for("guest"), mode=mode, run=sync_run))
        sh.show()
        spin(4)
        out.append(("auth_" + mode, sh))
    return out


def s_whats_new(ctx):
    import whats_new
    from ui.whats_new_window import WhatsNewWindow
    w = ctx.keep(WhatsNewWindow(whats_new.sections("2.1.3", "2.1.4"), current="2.1.4", previous="2.1.3",
                                release_url="https://example.invalid/releases/tag/v2.1.4", animate=False))
    w.show()
    settle(w)
    return [("whats_new", w, lambda: settle(w))]


def s_onboarding(ctx):
    from tests_qt.test_page_onboarding import app_for, sync_run
    from ui import onboarding_v2 as onb
    w = ctx.keep(onb.OnboardingWizard(app_for("guest"), run=sync_run, animate=False))
    w.show()
    spin()
    return [("welcome_" + name, w, lambda name=name: w.go(name)) for name in onb.PAGES]


def s_cards(ctx):
    from ui.components import mini_prompt as mp
    win = main_window(ctx)
    out = []
    specs = (("card_delete", dict(title="Delete “Q4 roadmap”?", body="Tue 29 Sep · 42 min", yes="Delete", no="Cancel",
                                  icon="trash", tone="danger", detail="Its notes, transcript and recording are removed.")),
             ("card_download", dict(title="Download Balanced · Whisper Small?", body="460 MB - Smart Actions will use it.",
                                    yes="Download", no="Not now", icon="download")),
             ("card_rename", dict(title="Rename meeting", body="Name", yes="Save", no="Cancel", icon="pen",
                                  field={"text": "Q4 roadmap"})))
    for name, kw in specs:
        d = mp.MiniPrompt(win, kw.pop("title"), kw.pop("body"), kw.pop("yes"), kw.pop("no"), **kw)
        ctx.keep(d)
        d.show()
        spin(4)
        out.append((name, d))
    return out


def s_answer(ctx):
    from ui.answer_card import AnswerCard
    card = ctx.keep(AnswerCard())
    card.show_text("**Yes.** It is polite.\n\n- warm\n- short\n\n`code` and a [link](https://example.com)", "answer",
                   sources=[{"title": "ECB euro reference rates", "url": "https://www.ecb.europa.eu/rates"}])
    spin(6)
    return [("answer_card", card)]


def s_reminder(ctx):
    from tests_qt.test_page_floating import NOW, full_app, meeting
    from ui.floating import meeting_reminder as mr
    t = ctx.keep(mr.MeetingReminder(full_app("pro"), clock=lambda: NOW))
    t.show_meeting(meeting())
    spin(6)
    return [("meeting_reminder", t)]


def s_notion(ctx):
    from tests_qt import test_notion_sheet as T
    import notion_connect
    ns = T.ns
    ctx.mp.setattr(ns, "_open_url", lambda url: True)
    ctx.mp.setattr(ns, "_run_then", T.sync_run)
    ctx.mp.setattr(notion_connect, "forget_tokens", lambda: None)
    ctx.mp.setattr(notion_connect, "latest_tokens", lambda: {})
    ctx.mp.setattr(notion_connect, "search_pages", lambda token, query="", limit=50: list(T.PAGES))
    out = []

    def build(cfg=None, run=None, **kw):
        sheet = ns.NotionSheet(T.Host(cfg), run=run or T.Deferred(), **kw)
        ctx.keep(sheet)
        sheet.show()
        spin()
        return sheet, None
    for state in ("intro", "connected_picker", "manual_error"):
        try:
            sheet = T._build_state(build, state)
            spin(4)
            out.append(("notion_" + state, sheet))
        except Exception:
            traceback.print_exc()
    return out


def s_live_card(ctx):
    from unittest import mock
    from tests_qt.test_live_card import _App
    from ui import glass, live_assist
    for name, val in (("capture_exclusion_supported", True), ("is_remote_session", False),
                      ("exclude_from_capture", True), ("is_excluded_from_capture", True),
                      ("macos_26_or_later", False)):
        ctx.mp.setattr(glass, name, mock.Mock(return_value=val))
    app = _App({"live_assist_theme": "system"})
    c = ctx.keep(live_assist.LiveAssistOverlay(main_app=app))
    c.resize(560, 460)
    c.show()
    spin(4)
    try:
        c.feed_transcript("Them: What does the yearly plan cost?", answer=False)
        c._on_partial("**290 euros a year** - about 24 a month.\n\n- cancel any time\n- one invoice", c._gen)
    except Exception:
        traceback.print_exc()
    spin(6)
    return [("live_card", c)]


def _invite_fake(ctx):
    """Invite friends' server is tests_qt/test_invite_ui.py's fake; calls run in place; no motion."""
    import invites
    from ui import invite_hub
    from ui.components import celebrate
    from tests_qt import test_invite_ui as T
    server = T.FakeServer()
    ctx.mp.setattr(invites, "_post", server)
    ctx.mp.setattr(invites, "device_hash", lambda: "d" * 64)
    ctx.mp.setattr(invite_hub.InviteHub, "sync_default", True)
    ctx.mp.setattr(celebrate, "_MOTION", {"forced": False})
    return server, T


def _invite_window(ctx, T, route="invite"):
    from ui import shell
    win = shell.MainWindow(T.stub("free"))
    win.resize(1280, 960)
    win.show()
    ctx.keep(win)
    spin()
    page = win.open(route)
    spin(10)
    return win, page


def s_invite(ctx):
    """The Invite page: two friends joined (and "Got a friend's code?"), a month ready (and the claim moment: the
    confetti a third of the way out and the toast), a month on, a subscriber's next bill free."""
    server, T = _invite_fake(ctx)
    out = []
    server.me = T.ME([T.MARIAM, T.DAVIT], can_join=True)
    win, _page = _invite_window(ctx, T)
    out.append(("invite_progress", win))
    server.me = T.READY
    win2, page2 = _invite_window(ctx, T)
    out.append(("invite_ready", win2))

    def claim_moment():
        from ui.components import celebrate
        celebrate._MOTION["forced"] = True
        try:
            page2.celebrate_claim("active", "18 Dec")
        finally:
            celebrate._MOTION["forced"] = False
        c = page2._last_confetti
        if c is not None:
            c.anim.stop()
            c._t = 0.33
            c.update()
        toast = celebrate.current_toast(win2)
        if toast is not None:
            toast.anim.stop()
            toast._rise = 1.0
            toast._place()
    out.append(("invite_claim_moment", win2, claim_moment))
    server.me = T.ACTIVE
    win3, _page3 = _invite_window(ctx, T)
    out.append(("invite_active", win3))
    server.me = T.CREDITED
    win4, _page4 = _invite_window(ctx, T)
    out.append(("invite_credited", win4))
    return out


def s_invite_join(ctx):
    """The invited friend (never on Home): Account's card with a friend's invite found, then joined; the Invite
    page's "Got a friend's code?" with nothing found (the field)."""
    server, T = _invite_fake(ctx)
    server.me = T.ME(can_join=True)
    server.answers["hint"] = {"found": True, "name": "Aram", "code": "aram-7k2q"}
    win, account = _invite_window(ctx, T, "account")
    out = [("account_invite_found", win)]
    server.after["join"] = lambda: setattr(server, "me", T.ME(invited_by={"name": "Aram"}))
    win2, account2 = _invite_window(ctx, T, "account")
    if account2.join_card is not None:
        account2.join_card.join_found.click()
        spin(6)
    out.append(("account_invite_joined", win2))
    server.after.pop("join", None)
    server.answers["hint"] = {"found": False}
    server.me = T.ME([T.MARIAM], can_join=True)
    win3, _page = _invite_window(ctx, T, "invite")
    out.append(("invite_code_field", win3))
    return out


SCREENS = (
    ("home", _page("home")),
    ("dictation", _page("dictation")),
    ("smart_actions", _page("smart_actions")),
    ("meetings", s_meetings),
    ("meeting_open", s_meeting_open),
    ("live_assistant", _page("live_assistant")),
    ("audio", s_audio),
    ("settings", s_settings),
    ("account", _page("account")),
    ("update_row", s_update_row),
    ("pro", s_pro),
    ("auth", s_auth),
    ("whats_new", s_whats_new),
    ("onboarding", s_onboarding),
    ("cards", s_cards),
    ("answer", s_answer),
    ("reminder", s_reminder),
    ("notion", s_notion),
    ("live_card", s_live_card),
    ("invite", s_invite),
    ("invite_join", s_invite_join),
)


# ═════════════════════════════ the run ═════════════════════════════
def contact_sheet(files, out_path, title):
    cols, tw = 5, 360
    cells = []
    for name, path in files:
        img = QImage(path)
        if img.isNull():
            continue
        th = img.scaledToWidth(tw, Qt.SmoothTransformation)
        cells.append((name, th))
    if not cells:
        return None
    rows = (len(cells) + cols - 1) // cols
    row_h = []
    for r in range(rows):
        row_h.append(max(th.height() for _n, th in cells[r * cols:(r + 1) * cols]) + 28)
    W = cols * (tw + 16) + 16
    H = 48 + sum(row_h) + 16
    sheet = QImage(W, H, QImage.Format_RGB32)
    sheet.fill(QColor("#7f8794"))
    p = QPainter(sheet)
    f = QFont("Segoe UI")
    f.setPixelSize(20)
    f.setBold(True)
    p.setFont(f)
    p.setPen(QColor("#ffffff"))
    p.drawText(QRect(16, 8, W - 32, 32), Qt.AlignVCenter | Qt.AlignLeft, title)
    f.setPixelSize(13)
    f.setBold(False)
    p.setFont(f)
    y = 48
    for r in range(rows):
        x = 16
        for name, th in cells[r * cols:(r + 1) * cols]:
            p.drawImage(QPoint(x, y), th)
            p.drawText(QRect(x, y + th.height() + 2, tw, 22), Qt.AlignLeft | Qt.AlignVCenter, name)
            x += tw + 16
        y += row_h[r]
    p.end()
    sheet.save(out_path)
    return out_path


def run_one(out, key, live=True):
    """One screen in every look, in this process (the child of run()). Writes <out>/manifest_<key>.json."""
    report = run(out, [key], live=live, sheets=False)
    return report


def run_isolated(out, only=None, live=True, timeout=120):
    """Every screen in a process of its own (a hang or a crash costs that screen only), then the sheets."""
    keys = [k for k, _b in SCREENS if not only or k in only]
    failed = {}
    for key in keys:
        cmd = [sys.executable, os.path.abspath(__file__), "--child", key, "--out", out] + ([] if live else ["--no-live"])
        t0 = time.perf_counter()
        try:
            res = subprocess.run(cmd, cwd=ROOT, timeout=timeout, capture_output=True, text=True,
                                 encoding="utf-8", errors="replace")
            ok = res.returncode == 0 and "!! " not in (res.stdout or "")
            if not ok:
                failed[key] = (res.stdout or "")[-1500:] + (res.stderr or "")[-1500:]
        except subprocess.TimeoutExpired:
            failed[key] = "timed out after %d s" % timeout
        print("%-16s %5.1fs %s" % (key, time.perf_counter() - t0, "ok" if key not in failed else "FAILED"), flush=True)
    for look in ["light", "dark"] + (["dark_live"] if live else []):
        files = []
        for key in keys:
            man = os.path.join(out, look, "manifest_%s.json" % key)
            if os.path.exists(man):
                with open(man, encoding="utf-8") as f:
                    files += [tuple(x) for x in json.load(f)]
        app = QApplication.instance() or QApplication([])
        contact_sheet(files, os.path.join(out, "contact_%s.png" % look), "Gusan - %s" % look)
    return failed


def run(out, only=None, live=True, sheets=True):
    app = QApplication.instance() or QApplication([])
    from ui import theme
    theme.load_fonts()
    looks = ["light", "dark"] + (["dark_live"] if live else [])
    tmp = tempfile.mkdtemp(prefix="gusan-sweep-data-")
    report = {}
    for look in looks:
        folder = os.path.join(out, look)
        os.makedirs(folder, exist_ok=True)
        theme.install_scheme(app, "light" if look in ("light", "dark_live") else "dark")
        files = []
        for key, build in SCREENS:
            if only and key not in only:
                continue
            mp = pytest.MonkeyPatch()
            ctx = Ctx(mp, tmp)
            t0 = time.perf_counter()
            try:
                _setup(ctx)
                shots = build(ctx)
                if look == "dark_live":
                    spin(2)
                    theme.switch("dark")
                    spin(6)
                for shot in shots:
                    name, w, prep = (tuple(shot) + (None,))[:3]
                    try:
                        if prep is not None:
                            prep()
                            spin(6)
                        files.append((name, grab(w, os.path.join(folder, name + ".png"))))
                    except Exception:
                        print("!! %s / %s failed:" % (look, name))
                        traceback.print_exc()
            except Exception:
                print("!! %s / %s failed:" % (look, key))
                traceback.print_exc()
                report.setdefault(look, []).append(key)
            finally:
                ctx.close_all()
                mp.undo()
                if look == "dark_live":
                    theme.switch("light")
            print("%-10s %-16s %.1fs" % (look, key, time.perf_counter() - t0), flush=True)
            if only and len(only) == 1:
                with open(os.path.join(folder, "manifest_%s.json" % only[0]), "w", encoding="utf-8") as f:
                    json.dump([[n, p] for n, p in files], f)
        if sheets:
            contact_sheet(files, os.path.join(out, "contact_%s.png" % look), "Gusan - %s" % look)
    theme.install_scheme(app, "light")
    return report


def main(argv=None):
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=DEFAULT_OUT)
    ap.add_argument("--only", default="")
    ap.add_argument("--no-live", action="store_true")
    ap.add_argument("--timeout", type=int, default=120, help="seconds one screen may take (all its looks)")
    ap.add_argument("--child", default="", help=argparse.SUPPRESS)
    a = ap.parse_args(argv)
    if a.child:
        report = run_one(a.out, a.child, live=not a.no_live)
        sys.stdout.flush()
        os._exit(1 if report else 0)                 # (no teardown: the next screen gets a fresh process)
    only = [s.strip() for s in a.only.split(",") if s.strip()] or None
    report = run_isolated(a.out, only, live=not a.no_live, timeout=a.timeout)
    if report:
        print("failed:")
        for k, v in report.items():
            print("  %s: %s" % (k, v.strip()[-600:]))
    print("contact sheets:", ", ".join(os.path.join(a.out, "contact_%s.png" % k) for k in ("light", "dark", "dark_live")))


if __name__ == "__main__":
    main()
