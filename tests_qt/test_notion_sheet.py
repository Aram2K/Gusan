# -*- coding: utf-8 -*-
"""The Notion sheet (ui/notion_sheet.py), offscreen: every screen, the one-button
connect flow (success, cancel, every error), the pasted-secret fallback, where
notes go, the automatic-send switch, Disconnect, the keyboard and the colours.

Fakes only: the browser, notion_connect.connect_mcp / disconnect / refresh and the
Notion calls are stand-ins, so nothing is opened or sent. Workers run through
a synchronous or a deferred runner (``run=``), like tests_qt/test_page_onboarding.py."""
import logging
import threading
from types import SimpleNamespace

import pytest
from PySide6.QtCore import QPoint, QRect, Qt, QTimer
from PySide6.QtGui import QColor, QPalette
from PySide6.QtWidgets import QAbstractButton, QApplication, QComboBox, QLabel, QLineEdit

import notion_connect
import notion_export
import notion_mcp
from notion_connect import ConnectError
from notion_export import NotionAuthError, NotionError
from ui import notion_sheet as ns
from ui import theme
from ui.winfit import fit_on_screen as _REAL_FIT        # the fixture below swaps it for a no-op

HUB = "99999999-9999-9999-9999-999999999999"
PAGES = [
    {"id": "11111111-1111-1111-1111-111111111111", "title": "Product meetings", "emoji": "📓", "url": "", "top": True},
    {"id": "22222222-2222-2222-2222-222222222222", "title": "Weekly sync notes", "emoji": "", "url": "", "top": True},
    {"id": "33333333-3333-3333-3333-333333333333", "title": "Roadmap 2027 planning with the whole leadership "
     "team and all our partners", "emoji": "", "url": "", "top": False},
]
TOKEN = {"access_token": "tok-1", "refresh_token": "ref-1", "workspace_id": "w1",
         "workspace_name": "Aram's workspace", "workspace_icon": "", "bot_id": "b1", "duplicated_template_id": ""}
# What the Connect button leaves behind (notion_mcp.connection_settings), and the older relay connection
# (a picker of shared pages; no build ships it switched on, but it still works).
MCP_CFG = {"notion_api_key": "tok-1", "notion_refresh_token": "ref-1", "notion_auth_mode": "mcp",
           "notion_workspace_name": "Aram's workspace", "notion_parent_page": HUB,
           "notion_parent_title": "Gusan meetings", "notion_hub_page": HUB, "notion_mcp_client_id": "cid-1",
           "notion_token_expires": 1900000000}
OAUTH_CFG = {"notion_api_key": "tok-1", "notion_refresh_token": "ref-1", "notion_auth_mode": "oauth",
             "notion_workspace_name": "Aram's workspace", "notion_parent_page": HUB,
             "notion_parent_title": "Gusan meetings", "notion_hub_page": HUB}
OWN_CFG = {"notion_api_key": "ntn_secret", "notion_auth_mode": "own", "notion_workspace_name": "",
           "notion_parent_page": PAGES[0]["id"], "notion_parent_title": PAGES[0]["title"]}
BLANK_KEYS = ("notion_api_key", "notion_refresh_token", "notion_auth_mode", "notion_workspace_name",
              "notion_parent_page", "notion_parent_title", "notion_hub_page", "notion_auto_send",
              "notion_reconnect", "notion_token_expires")
AUTHORIZE = "https://mcp.notion.com/authorize?client_id=cid-1&state=abc"


# ── fakes ────────────────────────────────────────────────────────────────────
def sync_run(fn, *args, then=None, error=None, **kwargs):
    try:
        res = fn(*args, **kwargs)
    except Exception as e:  # noqa: BLE001
        if error is not None:
            error(e)
        return None
    if then is not None:
        then(res)
    return None


class Deferred:
    """A runner that keeps the work; ``finish(i)`` runs job i and answers it."""

    def __init__(self):
        self.jobs = []

    def __call__(self, fn, *args, then=None, error=None, **kwargs):
        self.jobs.append(SimpleNamespace(fn=fn, args=args, then=then, error=error))

    def work(self, name):
        return [j for j in self.jobs if getattr(j.fn, "__name__", "") == name]

    def finish(self, i):
        job = self.jobs[i]
        try:
            res = job.fn(*job.args)
        except Exception as e:  # noqa: BLE001
            if job.error is not None:
                job.error(e)
            return
        if job.then is not None:
            job.then(res)


class Host:
    """What the sheet needs from the Meetings page."""

    def __init__(self, cfg=None):
        self.cfg_ = dict(cfg or {})
        self.events = []

    def cfg(self):
        return self.cfg_

    def window(self):
        return None

    def set_settings(self, updates):
        self.events.append(("set_settings", dict(updates)))
        self.cfg_.update(updates)
        return True

    def set_setting(self, key, value):
        self.events.append(("set_setting", key, value))
        self.cfg_[key] = value
        return True

    def track(self, event, props=None):
        self.events.append(("track", event, props))

    def call(self, method, *args):
        self.events.append(("call", method) + args)

    def tracked(self, name):
        return [e[2] for e in self.events if e[0] == "track" and e[1] == name]

    def saves(self):
        return [e[1] for e in self.events if e[0] == "set_settings"]

    def flushed(self):
        return ("call", "flush_config_save") in self.events


@pytest.fixture
def world(qapp, monkeypatch):
    """The browser, the grant calls and Notion replaced by stand-ins."""
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    monkeypatch.setattr(ui.winfit, "fit_on_screen", lambda *a, **k: None)      # the offscreen screen is 800x600
    w = SimpleNamespace(opened=[], revoked=[], disconnected=[], refreshed=[], connects=[], pages=list(PAGES),
                        search_outcomes=[], validate=lambda token: {"workspace_name": "My Notion"},
                        refresh_result=None, asked=[], answer=True, revoke_ok=True, registers=True)
    monkeypatch.setattr(ns, "_open_url", lambda url: w.opened.append(url) or True)
    monkeypatch.setattr(ns, "_run_then", sync_run)          # a sheet made without run= stays on this thread

    def disconnect(cfg):
        w.disconnected.append(dict(cfg))
        w.revoked.append(cfg.get("notion_api_key"))
        return w.revoke_ok
    monkeypatch.setattr(notion_connect, "disconnect", disconnect)
    monkeypatch.setattr(notion_connect, "revoke", lambda token: w.revoked.append(token) or w.revoke_ok)
    monkeypatch.setattr(notion_connect, "validate_token", lambda token: w.validate(token))

    def search(token, query="", limit=50):
        if w.search_outcomes:
            out = w.search_outcomes.pop(0)
            if isinstance(out, Exception):
                raise out
            return out
        return list(w.pages)
    monkeypatch.setattr(notion_connect, "search_pages", search)

    def refresh(refresh_token):
        w.refreshed.append(refresh_token)
        if isinstance(w.refresh_result, Exception):
            raise w.refresh_result
        return dict(w.refresh_result or dict(TOKEN, access_token="tok-2", refresh_token="ref-2"))
    monkeypatch.setattr(notion_connect, "refresh", refresh)
    monkeypatch.setattr(notion_connect, "forget_tokens", lambda: None)
    monkeypatch.setattr(notion_connect, "latest_tokens", lambda: {})

    def ask(parent, title, body, yes, no, icon="info"):
        w.asked.append((title, body, yes, no, icon))
        return w.answer
    monkeypatch.setattr(ns.mini_prompt, "ask", ask)
    return w


def connect_result(workspace="Aram's workspace"):
    """What notion_connect.connect_mcp returns (the core's own settings builder)."""
    res = {"mode": "mcp", "token": "tok-1", "refresh": "ref-1", "workspace": workspace, "expires_at": 1900000000,
           "client_id": "cid-1", "redirects": "2",
           "dest": {"id": HUB, "title": "Gusan meetings", "kind": "hub",
                                          "url": "https://www.notion.so/" + HUB.replace("-", "")},
           "pages": None}
    res["settings"] = notion_mcp.connection_settings(res)
    return res


def fake_connect(world, res=None, error=None, steps=True):
    """A notion_connect.connect_mcp stand-in (runs in the worker the runner gave it). Like the real
    one it gives a grant back itself when the user cancelled while it was being made."""
    def connect(cfg=None, open_browser=None, timeout=0, cancel=None, on_url=None, on_code=None, persist=None):
        world.connects.append({"cancel": cancel, "cfg": dict(cfg or {})})
        if on_url:
            on_url(AUTHORIZE)
        if open_browser:
            open_browser(AUTHORIZE)
        if error is not None:
            raise error
        if persist and world.registers:
            persist({"notion_mcp_client_id": "cid-1", "notion_mcp_redirects": "2"})
        if steps and on_code:
            on_code()
        out = res or connect_result()
        if cancel is not None and cancel.is_set():
            world.revoked.append(out["token"])
            raise notion_connect.ConnectCancelled()
        return out
    return connect


@pytest.fixture
def make(world):
    made = []

    def build(cfg=None, run=sync_run, connect=None, show=True, **kw):
        host = Host(cfg)
        sheet = ns.NotionSheet(host, run=run, connect_fn=connect or fake_connect(world), **kw)
        made.append(sheet)
        if show:
            sheet.show()
            QApplication.processEvents()
        return sheet, host
    yield build
    for s in made:
        try:
            s.flow.shutdown()
            s.hide()
            s.deleteLater()
        except RuntimeError:
            pass
    QApplication.processEvents()


# ── helpers ──────────────────────────────────────────────────────────────────
def _visible_controls(root):
    out = []
    for cls in (QAbstractButton, QLineEdit, QComboBox):
        for wd in root.findChildren(cls):
            if wd.isVisibleTo(root):
                out.append(wd)
    return out


def _name_of(wd):
    if wd.accessibleName().strip():
        return wd.accessibleName()
    if isinstance(wd, QAbstractButton):
        return wd.text().strip()
    return ""


def _texts(root):
    return [lb.text() for lb in root.findChildren(QLabel) if lb.isVisibleTo(root) and lb.text()]


def _all_text(root):
    """Every word a person can read or hear on the visible screen: labels, buttons, names."""
    out = _texts(root)
    for wd in _visible_controls(root):
        out += [wd.text() if isinstance(wd, QAbstractButton) else "", wd.accessibleName(), wd.toolTip()]
    for lb in root.findChildren(QLabel):
        if lb.isVisibleTo(root):
            out.append(lb.accessibleName())
    return " | ".join(t for t in out if t)


# The words of the plumbing: never on the first, waiting, error or connected screens.
JARGON = ("mcp", "oauth", "token", "integration", "api ", "client id", "authorize")


def _button(root, text):
    hits = [b for b in root.findChildren(QAbstractButton) if b.isVisibleTo(root) and b.text() == text]
    assert len(hits) == 1, "%r: %d visible" % (text, len(hits))
    return hits[0]


def _rgb(token):
    c = theme.qcolor(token)
    return c.red(), c.green(), c.blue()


def _lum(rgb):
    def ch(v):
        v /= 255.0
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in rgb)
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = sorted((_lum(a), _lum(b)), reverse=True)
    return (la + 0.05) / (lb + 0.05)


def _answer_all(run):
    """Answer every queued job once (a job that queues more is answered too)."""
    i = 0
    while i < len(run.jobs):
        run.finish(i)
        i += 1


# ── screen 1: the one button ─────────────────────────────────────────────────
def test_the_first_screen_is_one_button(make):
    sheet, _host = make()
    assert sheet.page_name() == "intro"
    assert sheet.property("gusan") == "window" and sheet.styleSheet() == theme.stylesheet()
    assert sheet.windowTitle() == "Notion"
    assert sheet.width() == ns.W and sheet.height() <= ns.MAX_H
    assert sheet.intro_title.text().replace("\n", " ") == "Send your meeting notes to Notion."
    assert sheet.intro_body.text() == "Gusan adds each meeting to a private page in your Notion. You choose when."
    primary = [b for b in sheet.findChildren(QAbstractButton)
               if b.isVisibleTo(sheet) and b.property("kind") == "primary"]
    assert [b.text() for b in primary] == ["Connect Notion"]
    # no field to fill in, no secret, no page link on the main path
    assert not [e for e in sheet.findChildren(QLineEdit) if e.isVisibleTo(sheet)]
    texts = " ".join(_texts(sheet))
    assert "Opens Notion in your browser." in texts
    for word in ("secret", "page link", "Page link", "coming soon", "share"):
        assert word not in texts, word
    # the own-secret route is one small quiet link at the bottom
    assert sheet.own_link.isVisibleTo(sheet) and sheet.own_link.text() == "Use your own Notion secret instead"
    assert sheet.own_link.property("kind") == "link"
    assert sheet.own_link.geometry().top() > sheet.connect_btn.geometry().bottom() - 1
    assert not sheet.intro_message.isVisibleTo(sheet)
    assert sheet.connect_btn.isDefault()           # Enter presses it


def test_the_plumbing_words_are_not_on_the_main_screens(world, make):
    run = Deferred()
    seen = {}
    sheet, _ = make(run=run)
    seen["intro"] = _all_text(sheet)
    sheet._show_intro("That took too long. Nothing was connected.")
    seen["error"] = _all_text(sheet)
    sheet._show_intro(notion_connect.MESSAGES["reconnect"])
    seen["reconnect"] = _all_text(sheet)
    sheet.connect_btn.click()
    seen["waiting"] = _all_text(sheet)
    sheet.flow.finishing.emit()
    seen["finishing"] = _all_text(sheet)
    for code in ("denied", "timeout", "offline", "port_busy", "expired", "server", "browser", "full", "other"):
        sheet._show_intro(notion_connect.MESSAGES[code])
        seen["error-" + code] = _all_text(sheet)
    connected, _ = make(MCP_CFG, run=run)
    seen["connected"] = _all_text(connected)
    for name, text in seen.items():
        assert text.strip(), name
        for word in JARGON:
            assert word not in text.lower(), (name, word, text)


def test_when_the_one_button_path_is_missing_the_sheet_opens_on_the_own_secret(world, make, monkeypatch):
    monkeypatch.setattr(notion_connect, "mcp_available", lambda: False)
    sheet, _host = make()
    assert sheet.page_name() == "manual"
    assert not sheet.back_btn.isVisibleTo(sheet)
    assert not sheet.connect_btn.isVisibleTo(sheet) and not sheet.own_link.isVisibleTo(sheet)
    everything = " ".join(_texts(sheet))
    assert "soon" not in everything.lower() and "Connect Notion" not in everything


def test_privacy_mode_is_asked_once_before_going_online(world, make):
    run = Deferred()
    sheet, host = make({"privacy_mode": True}, run=run)
    world.answer = False
    sheet.connect_btn.click()
    assert world.asked == [("Privacy mode is on", "This goes online. Connect anyway?", "Connect",
                            "Not now", "incognito")]
    assert sheet.page_name() == "intro" and not run.jobs and not world.connects
    world.answer = True
    sheet.connect_btn.click()
    assert sheet.page_name() == "waiting" and len(run.jobs) == 1


def test_reconnect_says_so_and_keeps_the_button_plain(make):
    sheet, _host = make(dict(MCP_CFG, notion_reconnect=True))
    assert sheet.page_name() == "intro"
    assert sheet.intro_message.isVisibleTo(sheet) and sheet.intro_message.text() == "Notion needs to be connected again."
    assert sheet.connect_btn.text() == "Connect Notion"
    sheet2, _ = make(MCP_CFG, reason="reconnect")
    assert sheet2.page_name() == "intro" and sheet2.intro_message.text() == "Notion needs to be connected again."


def test_reconnecting_goes_through_the_same_button_and_clears_the_flag(world, make):
    cfg = dict(MCP_CFG, notion_reconnect=True, notion_auto_send=True)
    sheet, host = make(cfg)
    sheet.connect_btn.click()
    assert sheet.page_name() == "connected"
    assert host.cfg()["notion_reconnect"] is False
    assert host.cfg()["notion_auto_send"] is True                      # their switch stays as it was
    assert world.connects[0]["cfg"]["notion_hub_page"] == HUB          # the core may keep the page it had
    assert world.connects[0]["cfg"]["notion_mcp_client_id"] == "cid-1"  # and reuses the id it registered


# ── screen 2: the browser, then connected ────────────────────────────────────
def test_waiting_then_finishing_then_connected(world, make):
    run = Deferred()
    sheet, host = make(run=run)
    sheet.connect_btn.click()
    assert sheet.page_name() == "waiting"
    assert sheet.wait_title.text() == "Finish in your browser"
    assert sheet.wait_body.text() == "Allow access in Notion, then come back here."
    assert sheet.reopen_link.isVisibleTo(sheet) and sheet.cancel_btn.isVisibleTo(sheet)
    assert sheet.reopen_link.text() == "Open it again" and sheet.cancel_btn.text() == "Cancel"
    assert sheet.progress.isVisibleTo(sheet) and sheet.progress._timer.isActive()
    assert not sheet.connect_btn.isVisibleTo(sheet)
    run.finish(0)                                          # the worker: the browser, the answer, the page
    assert world.opened == [AUTHORIZE]
    assert sheet.page_name() == "connected"
    assert not sheet.progress._timer.isActive()            # nothing keeps running behind the connected screen
    assert sheet.name_label.text() == "Aram's workspace"
    assert not sheet.auto_toggle.isChecked()               # a new connection starts quiet
    # nothing to pick, nothing to share, nothing to load
    assert sheet.hub_block.isVisibleTo(sheet) and not sheet.pick_block.isVisibleTo(sheet)
    assert sheet.hub_name.text() == "Gusan meetings"
    assert [j.fn.__name__ for j in run.jobs] == ["_connect_work"]
    assert sheet.done_btn.text() == "Done" and sheet.done_btn.isDefault()
    assert sheet.open_notion_btn.isVisibleTo(sheet) and sheet.open_notion_btn.text() == "Open Notion"
    # the whole connection in ONE write, and stored now
    saves = host.saves()
    assert saves[-1] == connect_result()["settings"]
    assert saves[-1] == {
        "notion_api_key": "tok-1", "notion_refresh_token": "ref-1", "notion_auth_mode": "mcp",
        "notion_workspace_name": "Aram's workspace", "notion_parent_page": HUB,
        "notion_parent_title": "Gusan meetings", "notion_hub_page": HUB, "notion_reconnect": False,
        "notion_mcp_client_id": "cid-1", "notion_mcp_redirects": "2", "notion_token_expires": 1900000000}
    assert host.flushed()
    assert host.tracked("notion_connect_result") == [{"mode": "mcp", "ok": True, "reason": ""}]
    assert notion_export.is_connected(host.cfg()) and notion_export.connection_mode(host.cfg()) == "mcp"


def test_the_registered_client_id_is_saved_the_moment_it_exists(world, make):
    run = Deferred()
    sheet, host = make(run=run)
    sheet.connect_btn.click()
    run.finish(0)
    saves = host.saves()
    assert saves[0] == {"notion_mcp_client_id": "cid-1", "notion_mcp_redirects": "2"}     # before the connection itself
    assert len(saves) == 2 and "notion_api_key" in saves[1]
    before_second = host.events[:host.events.index(("set_settings", saves[1]))]
    assert ("call", "flush_config_save") in before_second              # the id is stored at once, not on a timer


def test_a_worker_thread_hands_settings_to_the_gui_thread_and_only_notion_keys_get_through(world, make):
    """persist is called from the worker: the signal makes the save happen on the GUI thread, and only
    the connection's own keys ever get through."""
    import api_keys
    sheet, host = make()
    main = threading.get_ident()
    where = []
    orig = host.set_settings

    def spy(updates):
        where.append(threading.get_ident())
        return orig(updates)
    host.set_settings = spy
    t = api_keys.run_in_background(
        lambda: sheet.flow._persist.emit({"notion_mcp_client_id": "cid-9", "main_evil": 1, "privacy_mode": False}))
    t.join(10)
    for _ in range(50):
        QApplication.processEvents()
        if host.saves():
            break
    assert host.saves() == [{"notion_mcp_client_id": "cid-9"}]
    assert where == [main]


def test_the_code_arriving_shows_setting_things_up(world, make):
    run = Deferred()
    sheet, _host = make(run=run)
    sheet.connect_btn.click()
    sheet.flow.finishing.emit()
    assert sheet.wait_title.text() == "Setting things up…" and sheet.wait_body.text() == "Just a moment."
    assert not sheet.cancel_btn.isVisibleTo(sheet) and not sheet.reopen_link.isVisibleTo(sheet)
    assert sheet.progress.isVisibleTo(sheet)


def test_open_it_again_uses_the_same_address(world, make):
    run = Deferred()
    sheet, _host = make(run=run)
    sheet.connect_btn.click()
    sheet.flow._set_url("https://mcp.notion.com/authorize?client_id=cid-1&state=abc.5555")
    sheet.reopen_link.click()
    sheet.reopen_link.click()
    assert world.opened == ["https://mcp.notion.com/authorize?client_id=cid-1&state=abc.5555"] * 2
    assert sheet.reopen_link.accessibleName() == "Open Notion again in your browser"


def test_cancel_goes_back_quietly_and_a_late_answer_is_given_back(world, make):
    run = Deferred()
    sheet, host = make(run=run)
    sheet.connect_btn.click()
    sheet.cancel_btn.click()
    assert sheet.page_name() == "intro" and not sheet.intro_message.isVisibleTo(sheet)
    assert sheet.connect_btn.text() == "Connect Notion"
    assert isinstance(run.jobs[0].args[0], threading.Event) and run.jobs[0].args[0].is_set()
    assert host.tracked("notion_connect_result") == [{"mode": "mcp", "ok": False, "reason": "cancelled"}]
    run.finish(0)                                          # the worker only now ends: its answer is ignored
    assert sheet.page_name() == "intro"
    assert not [u for u in host.saves() if "notion_api_key" in u] and host.cfg().get("notion_api_key") is None
    assert world.revoked == ["tok-1"]                      # ...and the grant it made is given back


def test_cancel_keeps_saying_the_connection_needs_connecting_again(world, make):
    """A connection Notion refused for good: pressing Cancel while reconnecting goes back to the
    first screen, which still says why it is there (not a screen that looks like a fresh start)."""
    run = Deferred()
    sheet, _host = make(dict(MCP_CFG, notion_reconnect=True), run=run)
    assert sheet.intro_message.text() == "Notion needs to be connected again."
    sheet.connect_btn.click()
    assert sheet.page_name() == "waiting"
    sheet.cancel_btn.click()
    assert sheet.page_name() == "intro"
    assert sheet.intro_message.isVisibleTo(sheet) and sheet.intro_message.text() == "Notion needs to be connected again."
    assert sheet.connect_btn.text() == "Connect Notion"                    # not "Try again": nothing failed


def test_a_grant_that_arrives_after_giving_up_is_given_back_whole(world, make):
    run = Deferred()
    sheet, host = make(run=run)
    sheet.connect_btn.click()
    job = run.jobs[0]
    res = job.fn(threading.Event(), {})                    # the worker finished first (never cancelled)...
    sheet.cancel_btn.click()                               # ...the user gave up before its answer was read
    job.then(res)
    assert run.jobs[-1].fn is notion_connect.disconnect    # giving the grant back is queued as work
    run.finish(len(run.jobs) - 1)
    assert world.revoked == ["tok-1"] and sheet.page_name() == "intro"
    given = world.disconnected[0]                          # everything that is needed to revoke it at Notion
    assert (given["notion_auth_mode"], given["notion_refresh_token"], given["notion_mcp_client_id"]) == (
        "mcp", "ref-1", "cid-1")
    assert not [u for u in host.saves() if "notion_api_key" in u]


def test_closing_the_sheet_while_waiting_cancels_it(world, make):
    run = Deferred()
    sheet, host = make(run=run)
    sheet.connect_btn.click()
    sheet.reject()
    assert run.jobs[0].args[0].is_set()
    assert host.tracked("notion_connect_result") == [{"mode": "mcp", "ok": False, "reason": "cancelled"}]
    run.finish(0)
    assert not [u for u in host.saves() if "notion_api_key" in u] and host.cfg().get("notion_api_key") is None
    assert world.revoked == ["tok-1"]
    assert len(host.tracked("notion_connect_result")) == 1          # the late answer adds nothing


@pytest.mark.parametrize("code", ["denied", "timeout", "offline", "port_busy", "expired", "server", "browser", "full",
                                  "other"])
def test_every_failure_is_one_plain_sentence_on_the_first_screen(world, make, code):
    sheet, host = make(connect=fake_connect(world, error=ConnectError(code)))
    sheet.connect_btn.click()
    assert sheet.page_name() == "intro"
    assert sheet.intro_message.isVisibleTo(sheet)
    assert sheet.intro_message.text() == notion_connect.MESSAGES[code]
    assert sheet.connect_btn.text() == "Try again"
    assert not [u for u in host.saves() if "notion_api_key" in u]
    assert host.tracked("notion_connect_result") == [{"mode": "mcp", "ok": False, "reason": code}]


def test_exact_failure_sentences(world, make):
    expect = {"denied": "You cancelled in Notion. Nothing was connected.",
              "timeout": "That took too long. Nothing was connected.",
              "offline": "Can't reach Notion. Check your internet and try again.",
              "port_busy": "Couldn't start the connection on this computer. Close other Gusan windows and try "
                           "again.",
              "expired": "That Notion sign-in expired. Try again.",
              "server": "Notion couldn't finish connecting. Try again in a minute.",
              "browser": "Couldn't open your browser. Try again.",
              "full": "That Notion workspace is full. Free up space or upgrade it.",
              "other": "Couldn't connect to Notion. Try again."}
    for code, sentence in expect.items():
        assert notion_connect.MESSAGES[code] == sentence


def test_a_full_workspace_is_said_in_full_on_the_first_screen_and_nothing_is_kept(world, make):
    """Connected, but Notion cannot make even the first page (a free workspace with several members has a
    total limit on blocks): the core gives the grant back and the sheet says so, plainly, with a way out."""
    sheet, host = make(connect=fake_connect(world, error=ConnectError("full")))
    sheet.connect_btn.click()
    assert sheet.page_name() == "intro" and sheet.intro_message.isVisibleTo(sheet)
    text = sheet.intro_message.text()
    assert text == notion_export.FULL_TEXT
    assert text.startswith("That Notion workspace is full.") and text.endswith("upgrade it.")
    for word in JARGON + ("block", "mcp", "error"):
        assert word not in text.lower(), word
    assert sheet.connect_btn.text() == "Try again"                       # one click, once the user has made room
    label = sheet.intro_message
    assert label.heightForWidth(label.width()) <= label.height() + 1, "the sentence is cut off"
    assert sheet.height() <= ns.MAX_H and sheet.scroll.verticalScrollBar().maximum() == 0   # no scrolling to read it
    assert not [u for u in host.saves() if "notion_api_key" in u] and host.cfg().get("notion_api_key") is None
    assert host.tracked("notion_connect_result") == [{"mode": "mcp", "ok": False, "reason": "full"}]
    # and the same click, with room made, connects
    sheet.flow._connect_fn = fake_connect(world)
    sheet.connect_btn.click()
    assert sheet.page_name() == "connected"


def test_trying_again_clears_the_message(world, make):
    sheet, _host = make(connect=fake_connect(world, error=ConnectError("timeout")))
    sheet.connect_btn.click()
    assert sheet.intro_message.isVisibleTo(sheet)
    sheet.flow._connect_fn = fake_connect(world)
    sheet.connect_btn.click()
    assert sheet.page_name() == "connected" and not sheet.intro_message.isVisibleTo(sheet)


def test_a_failed_save_is_a_plain_failure_not_a_half_connection(world, make):
    sheet, host = make()
    host.set_settings = lambda updates: "notion_api_key" not in updates
    sheet.connect_btn.click()
    assert sheet.page_name() == "intro" and sheet.intro_message.text() == notion_connect.MESSAGES["other"]


def test_connecting_in_privacy_mode_asks_once_and_goes_online_for_nothing_else(world, make):
    run = Deferred()
    sheet, host = make({"privacy_mode": True}, run=run)
    sheet.connect_btn.click()                                       # asked here: yes
    run.finish(0)
    assert len(world.asked) == 1 and sheet.page_name() == "connected"
    assert [j.fn.__name__ for j in run.jobs] == ["_connect_work"]   # no page search behind the connected screen


# ── the keyboard ─────────────────────────────────────────────────────────────
def test_enter_connects_and_esc_cancels_the_wait_then_closes(world, make):
    from PySide6.QtTest import QTest
    run = Deferred()
    sheet, host = make(run=run)
    sheet.activateWindow()
    QApplication.processEvents()
    QTest.keyClick(sheet, Qt.Key_Return)                            # Enter = the primary button
    assert sheet.page_name() == "waiting" and len(run.jobs) == 1
    QTest.keyClick(sheet, Qt.Key_Escape)                            # Esc = Cancel, the sheet stays
    assert sheet.page_name() == "intro" and sheet.isVisible()
    assert run.jobs[0].args[0].is_set()
    assert host.tracked("notion_connect_result") == [{"mode": "mcp", "ok": False, "reason": "cancelled"}]
    QTest.keyClick(sheet, Qt.Key_Escape)                            # a second Esc closes it
    assert not sheet.isVisible()


def test_enter_twice_never_cancels_and_the_focus_is_on_a_harmless_control(world, make):
    from PySide6.QtTest import QTest
    run = Deferred()
    sheet, _host = make(run=run)
    sheet.activateWindow()
    QApplication.processEvents()
    QTest.keyClick(sheet, Qt.Key_Return)
    QApplication.processEvents()
    assert sheet.page_name() == "waiting" and sheet.focusWidget() is sheet.reopen_link
    QTest.keyClick(sheet, Qt.Key_Return)                            # a second Enter re-opens the browser...
    assert sheet.page_name() == "waiting" and not run.jobs[0].args[0].is_set()      # ...and cancels nothing
    sheet.flow.finishing.emit()
    QTest.keyClick(sheet, Qt.Key_Escape)                            # "Setting things up…" has no Cancel: Esc closes
    assert not sheet.isVisible() and run.jobs[0].args[0].is_set()


def _tab_stops(sheet):
    """The widgets Tab visits on the screen showing, in order (walking the real focus chain)."""
    first = sheet.stack.currentWidget()
    seen, w = [], sheet
    for _ in range(200):
        w = w.nextInFocusChain()
        if w is sheet or (seen and w is seen[0]):
            break
        if (w.focusPolicy() & Qt.TabFocus) and w.isVisibleTo(sheet) and first.isAncestorOf(w):
            seen.append(w)
    return seen


def test_tab_order_follows_reading_order_and_every_stop_has_a_name(world, make):
    run = Deferred()
    intro, _ = make(run=run)
    assert [_name_of(w) for w in _tab_stops(intro)] == ["Connect Notion", "Use your own Notion secret instead"]
    intro.connect_btn.click()
    assert [_name_of(w) for w in _tab_stops(intro)] == ["Open Notion again in your browser", "Cancel"]
    connected, _ = make(MCP_CFG, run=run)
    assert [_name_of(w) for w in _tab_stops(connected)] == [
        "Send every meeting's notes to Notion automatically", "Disconnect", "Open Notion", "Done"]
    with_pages, _ = make(OAUTH_CFG, run=run)
    assert [_name_of(w) for w in _tab_stops(with_pages)] == [
        "Send every meeting's notes to Notion automatically", "Notes go to", "Refresh pages", "Disconnect",
        "Open Notion", "Done"]
    manual, _ = make(run=run)
    manual.own_link.click()
    assert [_name_of(w) for w in _tab_stops(manual)] == [
        "Back", "Open Notion integrations", "Integration secret", "Show secret", "Connect"]
    manual._on_no_page({"mode": "own", "token": "t", "refresh": "", "workspace": ""})
    assert [_name_of(w) for w in _tab_stops(manual)] == ["Open Notion", "Check again", "Cancel"]


# ── screen 4: the pasted secret ──────────────────────────────────────────────
def _manual(make, cfg=None, run=sync_run, **kw):
    sheet, host = make(cfg, run=run, **kw)
    sheet.own_link.click()
    assert sheet.page_name() == "manual"
    return sheet, host


def test_manual_screen_content(world, make):
    sheet, _host = _manual(make)
    assert sheet.back_btn.isVisibleTo(sheet)
    assert [b.text() for b in sheet.findChildren(QAbstractButton) if b.isVisibleTo(sheet)
            and b.text() in ("Open Notion integrations", "Connect")] == ["Open Notion integrations", "Connect"]
    assert not sheet.manual_btn.isEnabled()                   # nothing pasted yet
    assert sheet.secret.edit.placeholderText() == "Starts with ntn_ or secret_"
    assert sheet.secret.eye.accessibleName() == "Show secret"
    sheet.secret.toggle()
    assert sheet.secret.eye.accessibleName() == "Hide secret"
    assert not [e for e in sheet.findChildren(QLineEdit) if e.isVisibleTo(sheet) and e is not sheet.secret.edit]
    _button(sheet, "Open Notion integrations").click()
    assert world.opened[-1] == notion_connect.INTEGRATIONS_URL
    sheet.back_btn.click()
    assert sheet.page_name() == "intro"


def test_manual_connect_writes_the_connection_and_preselects_the_first_page(world, make):
    sheet, host = _manual(make)
    sheet.secret.edit.setText("  ntn_abc  ")
    assert sheet.manual_btn.isEnabled()
    sheet.manual_btn.click()
    assert sheet.page_name() == "connected"
    assert host.cfg()["notion_api_key"] == "ntn_abc"          # trimmed
    assert host.cfg()["notion_auth_mode"] == "own"
    assert host.cfg()["notion_workspace_name"] == "My Notion"
    assert host.cfg()["notion_parent_page"] == PAGES[0]["id"] and host.cfg()["notion_hub_page"] == ""
    assert host.cfg()["notion_refresh_token"] == "" and host.cfg()["notion_token_expires"] == 0
    assert sheet.name_label.text() == "My Notion"
    assert sheet.pick_block.isVisibleTo(sheet) and not sheet.hub_block.isVisibleTo(sheet)
    assert sheet.dest_hint.text() == "To add pages, share them with your integration in Notion."
    assert sheet.picker.itemText(0).endswith("Product meetings") and "private page" not in sheet.picker.itemText(0)
    assert host.tracked("notion_connect_result") == [{"mode": "own", "ok": True, "reason": ""}]
    assert not sheet.secret.edit.text()                       # the secret is not left in the field


def test_manual_a_refused_secret_is_a_red_line_and_nothing_is_saved(world, make):
    def refuse(token):
        raise NotionAuthError("401")
    world.validate = refuse
    sheet, host = _manual(make)
    sheet.secret.edit.setText("ntn_wrong")
    sheet.manual_btn.click()
    assert sheet.page_name() == "manual"
    assert sheet.manual_message.isVisibleTo(sheet)
    assert sheet.manual_message.text() == "Notion didn't accept that secret. Copy it again from your integration."
    assert sheet.secret.box.has_error()
    assert sheet.manual_btn.text() == "Connect" and sheet.manual_btn.isEnabled()
    assert not host.saves()
    assert host.tracked("notion_connect_result") == [{"mode": "own", "ok": False, "reason": "other"}]
    sheet.secret.edit.setText("ntn_wrong2")                   # typing again clears the red
    assert not sheet.manual_message.isVisibleTo(sheet) and not sheet.secret.box.has_error()


def test_manual_offline_has_its_own_sentence(world, make):
    def offline(token):
        raise NotionError("x", code="offline")
    world.validate = offline
    sheet, _host = _manual(make)
    sheet.secret.edit.setText("ntn_x")
    sheet.manual_btn.click()
    assert sheet.manual_message.text() == "Can't reach Notion. Check your internet and try again."
    assert not sheet.secret.box.has_error()                   # the secret may well be fine


def test_manual_checking_state_while_it_runs(world, make):
    run = Deferred()
    sheet, _host = _manual(make, run=run)
    sheet.secret.edit.setText("ntn_x")
    sheet.manual_btn.click()
    assert sheet.manual_btn.text() == "Checking…" and not sheet.manual_btn.isEnabled()
    assert sheet.secret.edit.isReadOnly()
    sheet._back()                                             # Back does nothing while it checks
    assert sheet.page_name() == "manual"
    run.finish(0)
    assert sheet.page_name() == "connected" and not sheet.secret.edit.isReadOnly()


def test_manual_with_no_shared_page_asks_to_share_one_and_saves_nothing(world, make):
    world.pages = []
    sheet, host = _manual(make)
    sheet.secret.edit.setText("ntn_x")
    sheet.manual_btn.click()
    assert sheet.page_name() == "share"
    assert sheet.share_body.accessibleName().endswith("add your integration. Then come back here.")
    assert not host.saves() and host.cfg().get("notion_api_key") is None      # held in memory only
    world.pages = list(PAGES)
    sheet.check_btn.click()
    assert sheet.page_name() == "connected" and host.cfg()["notion_api_key"] == "ntn_x"
    assert host.cfg()["notion_auth_mode"] == "own"


def test_manual_cancel_on_share_discards_the_secret_and_never_calls_our_server(world, make):
    world.pages = []
    sheet, host = _manual(make)
    sheet.secret.edit.setText("ntn_x")
    sheet.manual_btn.click()
    _button(sheet, "Cancel").click()
    assert not host.saves() and world.revoked == []           # an own secret never goes to the function


def test_share_screen_content_and_check_again_with_nothing_shared(world, make):
    world.pages = []
    sheet, host = _manual(make)
    sheet.secret.edit.setText("ntn_x")
    sheet.manual_btn.click()
    assert sheet.page_name() == "share"
    assert sheet.share_body.accessibleName().startswith(
        "In Notion, open the page for your notes, click ••• then Connections, and add your integration.")
    assert "<b" in sheet.share_body.text()                    # the things to click are in bold
    assert not sheet.share_message.isVisibleTo(sheet) and not sheet.reopen_link.isVisibleTo(sheet)
    for text in ("Open Notion", "Check again", "Cancel"):
        assert _button(sheet, text)
    _button(sheet, "Open Notion").click()
    assert world.opened[-1] == notion_connect.NOTION_HOME_URL
    sheet.check_btn.click()                                   # still nothing shared: say so
    assert sheet.page_name() == "share" and sheet.share_message.isVisibleTo(sheet)
    assert sheet.share_message.text() == "Nothing shared yet. Add the page, then check again."
    assert sheet.check_btn.text() == "Check again" and sheet.check_btn.isEnabled()
    assert not host.saves()


def test_a_grant_held_without_a_page_is_given_back_when_the_sheet_closes(world, make):
    """Only a button grant is ever given back this way; a pasted secret never leaves for our server."""
    sheet, host = make()
    sheet._on_no_page({"mode": "mcp", "token": "tok-held", "refresh": "ref-held", "workspace": "W",
                       "client_id": "cid-1"})
    assert sheet.page_name() == "share"
    sheet.reject()
    assert world.revoked == ["tok-held"] and world.disconnected[0]["notion_refresh_token"] == "ref-held"
    assert not host.saves()


# ── screen 3: connected ──────────────────────────────────────────────────────
def test_a_button_connection_has_nothing_to_pick_and_goes_nowhere_by_itself(world, make):
    run = Deferred()
    sheet, host = make(MCP_CFG, run=run)
    assert sheet.page_name() == "connected"                   # already connected: straight here
    assert sheet.name_label.text() == "Aram's workspace"
    assert not run.jobs and not world.asked                   # no search, no refresh, nothing online
    assert sheet.hub_block.isVisibleTo(sheet) and not sheet.pick_block.isVisibleTo(sheet)
    assert not sheet.picker.isVisibleTo(sheet) and not sheet.refresh_btn.isVisibleTo(sheet)
    assert not sheet.search_row.isVisibleTo(sheet)
    texts = " ".join(_texts(sheet))
    assert "Notes go to" in texts and "Gusan meetings" in texts and "A private page in your Notion." in texts
    assert "share" not in texts.lower() and "integration" not in texts.lower()
    assert "Send every meeting automatically" in texts
    assert not sheet.auto_toggle.isChecked()
    names = [_name_of(b) for b in sheet.findChildren(QAbstractButton) if b.isVisibleTo(sheet)]
    assert "Disconnect" in names and "Open Notion" in names and "Done" in names


def test_a_button_connection_in_privacy_mode_asks_nothing(world, make):
    run = Deferred()
    sheet, host = make(dict(MCP_CFG, privacy_mode=True), run=run)
    assert sheet.page_name() == "connected" and not run.jobs and world.asked == []
    assert sheet.hub_block.isVisibleTo(sheet)
    assert ns.DEST_HINT_PAUSED not in _all_text(sheet)        # nothing is paused: nothing goes online


def test_open_notion_opens_the_page_the_notes_go_to(world, make):
    sheet, _host = make(MCP_CFG)
    sheet.open_notion_btn.click()
    assert world.opened[-1] == "https://www.notion.so/" + HUB.replace("-", "")
    own, _ = make(OWN_CFG)
    own.open_notion_btn.click()
    assert world.opened[-1] == "https://www.notion.so/" + PAGES[0]["id"].replace("-", "")
    blank, _ = make(dict(MCP_CFG, notion_parent_page=""))
    blank.open_notion_btn.click()
    assert world.opened[-1] == notion_connect.NOTION_HOME_URL


def test_connected_picker_lists_the_hub_then_the_shared_pages(world, make):
    run = Deferred()
    sheet, host = make(OAUTH_CFG, run=run)
    assert sheet.page_name() == "connected"                   # already connected: straight here
    assert sheet.name_label.text() == "Aram's workspace"
    assert [j.fn.__name__ for j in run.jobs] == ["_pages_work"]       # the pages load in the background
    assert sheet.picker.count() == 1 and sheet.picker.itemText(0) == "Gusan meetings (private page)"
    run.finish(0)
    items = [sheet.picker.itemText(i) for i in range(sheet.picker.count())]
    assert items[0] == "Gusan meetings (private page)"
    assert items[1] == "📓 Product meetings" and items[2] == "Weekly sync notes"
    assert len(items[3]) <= ns.PICK_CHARS and items[3].endswith("…") and items[3].startswith("Roadmap 2027")
    assert sheet.picker.currentIndex() == 0
    assert sheet.dest_hint.text() == "To add pages, share them with Gusan in Notion."


def test_picking_a_page_writes_it_at_once(world, make):
    sheet, host = make(OAUTH_CFG)
    sheet.picker.setCurrentIndex(2)
    sheet.picker.activated.emit(2)
    assert host.cfg()["notion_parent_page"] == PAGES[1]["id"]
    assert host.cfg()["notion_parent_title"] == "Weekly sync notes"
    assert host.cfg()["notion_hub_page"] == HUB               # the hub stays known
    assert host.flushed()
    sheet.picker.setCurrentIndex(0)
    sheet.picker.activated.emit(0)
    assert host.cfg()["notion_parent_page"] == HUB and host.cfg()["notion_parent_title"] == "Gusan meetings"


def test_the_saved_destination_stays_in_the_picker_when_it_is_not_in_the_search(world, make):
    cfg = dict(OAUTH_CFG, notion_parent_page="44444444-4444-4444-4444-444444444444",
               notion_parent_title="Old project")
    sheet, _host = make(cfg)
    texts = [sheet.picker.itemText(i) for i in range(sheet.picker.count())]
    assert "Old project" in texts and sheet.picker.currentText() == "Old project"


def test_a_failed_search_says_so_without_losing_the_saved_page(world, make):
    world.search_outcomes = [NotionError("x", code="offline")]
    sheet, host = make(OAUTH_CFG)
    assert sheet.dest_hint.text() == "Couldn't load your pages. Try refresh."
    assert sheet.picker.itemText(0) == "Gusan meetings (private page)"
    sheet.refresh_btn.click()
    assert sheet.dest_hint.text() == "To add pages, share them with Gusan in Notion."
    assert sheet.picker.count() == 1 + len(PAGES)
    assert host.cfg()["notion_api_key"] == "tok-1"


@pytest.mark.parametrize("cfg", [MCP_CFG, OAUTH_CFG, OWN_CFG])
def test_the_switch_writes_at_once_and_starts_from_what_is_saved(world, make, cfg):
    sheet, host = make(dict(cfg, notion_auto_send=True))
    assert sheet.auto_toggle.isChecked()
    sheet.auto_toggle.click()
    assert ("set_setting", "notion_auto_send", False) in host.events
    sheet.auto_toggle.click()
    assert host.cfg()["notion_auto_send"] is True
    assert sheet.auto_toggle.accessibleName() == "Send every meeting's notes to Notion automatically"


def test_opening_a_connected_sheet_from_send_this_meeting(world, make):
    sheet, _host = make(MCP_CFG, then="Send this meeting")
    assert sheet.done_btn.text() == "Send this meeting"
    sheet.done_btn.click()
    assert sheet.result_sent is True
    plain, _ = make(MCP_CFG)
    plain.done_btn.click()
    assert plain.result_sent is False


def test_a_long_workspace_name_is_elided_not_wrapped(world, make):
    name = "Institut Imagine – Direction des systèmes d'information et du numérique"
    sheet, _host = make(dict(MCP_CFG, notion_workspace_name=name))
    assert sheet.name_label.full_text() == name
    assert sheet.name_label.text() != name and sheet.name_label.text().endswith("…")
    assert sheet.name_label.height() < 30
    assert sheet.height() <= ns.MAX_H and sheet.width() == ns.W


def test_a_legacy_connection_reads_as_connected_with_the_plain_name(world, make):
    sheet, _host = make({"notion_api_key": "ntn_old", "notion_parent_page": PAGES[0]["id"]})
    assert sheet.page_name() == "connected" and sheet.name_label.text() == "Notion"
    assert sheet.dest_hint.text() == "To add pages, share them with your integration in Notion."
    assert "private page" not in " ".join(sheet.picker.itemText(i) for i in range(sheet.picker.count()))


# ── disconnect ───────────────────────────────────────────────────────────────
@pytest.mark.parametrize("cfg,mode", [(MCP_CFG, "mcp"), (OAUTH_CFG, "oauth")])
def test_disconnect_forgets_at_once_and_gives_the_grant_back(world, make, cfg, mode):
    sheet, host = make(dict(cfg, notion_auto_send=True))
    sheet.disconnect_link.click()
    for key in BLANK_KEYS:
        assert host.cfg()[key] in ("", False, 0), key
    assert host.cfg()["notion_auto_send"] is False and host.cfg()["notion_reconnect"] is False
    assert world.revoked == ["tok-1"]
    given = world.disconnected[0]                              # the settings as they were BEFORE they were blanked
    assert given["notion_auth_mode"] == mode and given["notion_refresh_token"] == "ref-1"
    assert host.tracked("notion_disconnected") == [{"mode": mode, "reason": "user"}]
    assert host.flushed()
    assert sheet.page_name() == "intro" and not sheet.intro_message.isVisibleTo(sheet)


def test_disconnecting_a_button_connection_keeps_the_id_gusan_registered_and_connects_again(world, make):
    """Registering again would orphan earlier grants: the id outlives a disconnect and is reused."""
    sheet, host = make(MCP_CFG)
    sheet.disconnect_link.click()
    assert host.cfg()["notion_mcp_client_id"] == "cid-1"
    assert world.disconnected[0]["notion_mcp_client_id"] == "cid-1"
    sheet.connect_btn.click()                                  # the same sheet connects again
    assert sheet.page_name() == "connected" and host.cfg()["notion_api_key"] == "tok-1"
    assert world.connects[0]["cfg"]["notion_mcp_client_id"] == "cid-1"
    assert world.connects[0]["cfg"].get("notion_api_key") in ("", None)


def test_disconnect_of_an_own_secret_never_leaves_for_our_server(world, make):
    sheet, host = make(OWN_CFG)
    sheet.disconnect_link.click()
    assert world.revoked == []
    assert host.cfg()["notion_api_key"] == "" and host.cfg()["notion_auth_mode"] == ""
    assert host.tracked("notion_disconnected") == [{"mode": "own", "reason": "user"}]


def test_disconnect_while_the_pages_load_is_not_undone_by_their_answer(world, make):
    run = Deferred()
    sheet, host = make(OAUTH_CFG, run=run)                    # a pages job is queued
    world.search_outcomes = [NotionAuthError("401")]
    world.refresh_result = dict(TOKEN, access_token="tok-9", refresh_token="ref-9")
    sheet.disconnect_link.click()
    run.finish(0)                                             # the pages job answers late
    assert host.cfg()["notion_api_key"] == ""                 # still disconnected
    assert sheet.page_name() == "intro"


def test_disconnect_notion_function_prefers_the_newest_pair(world, monkeypatch):
    monkeypatch.setattr(notion_connect, "latest_tokens",
                        lambda: {"access_token": "tok-newest", "refresh_token": "ref-newest"})
    host = Host(MCP_CFG)
    ns.disconnect_notion(host, run=sync_run)
    assert world.revoked == ["tok-newest"] and world.disconnected[0]["notion_refresh_token"] == "ref-newest"
    assert host.cfg()["notion_api_key"] == ""


def test_disconnect_when_never_connected_does_nothing_harmful(world):
    host = Host({})
    ns.disconnect_notion(host, run=sync_run)
    assert world.revoked == []
    assert host.tracked("notion_disconnected") == [{"mode": "own", "reason": "user"}]


# ── the connection ending later ──────────────────────────────────────────────
def test_a_refused_token_is_refreshed_once_and_the_new_pair_saved(world, make):
    world.search_outcomes = [NotionAuthError("401")]
    sheet, host = make(OAUTH_CFG)
    assert world.refreshed == ["ref-1"]
    assert host.cfg()["notion_api_key"] == "tok-2" and host.cfg()["notion_refresh_token"] == "ref-2"
    assert sheet.page_name() == "connected" and sheet.picker.count() == 1 + len(PAGES)


def test_a_refresh_notion_refuses_for_good_asks_to_connect_again(world, make):
    world.search_outcomes = [NotionAuthError("401")]
    world.refresh_result = ConnectError("expired")
    sheet, host = make(OAUTH_CFG)
    assert host.cfg()["notion_reconnect"] is True
    assert sheet.page_name() == "intro"
    assert sheet.intro_message.text() == "Notion needs to be connected again."
    assert sheet.connect_btn.text() == "Connect Notion"


def test_an_own_secret_notion_refuses_asks_for_a_new_one(world, make):
    world.search_outcomes = [NotionAuthError("401")]
    sheet, host = make(OWN_CFG)
    assert world.refreshed == []                              # no refresh for a pasted secret
    assert not host.cfg().get("notion_reconnect")             # and no "connect again" for it
    assert sheet.page_name() == "manual"
    assert sheet.manual_message.text() == "Notion didn't accept that secret. Copy it again from your integration."


# ── show_notion_sheet ────────────────────────────────────────────────────────
def test_show_notion_sheet_returns_what_the_caller_asked(world):
    host = Host(MCP_CFG)
    QTimer.singleShot(30, lambda: ns._SHEET["sheet"].done_btn.click())
    assert ns.show_notion_sheet(host) is True                  # no `then`: connected as it closed
    QTimer.singleShot(30, lambda: ns._SHEET["sheet"].done_btn.click())
    assert ns.show_notion_sheet(host, then="Send this meeting") is True
    QTimer.singleShot(30, lambda: ns._SHEET["sheet"].reject())
    assert ns.show_notion_sheet(host, then="Send this meeting") is False
    gone = Host({})
    QTimer.singleShot(30, lambda: ns._SHEET["sheet"].reject())
    assert ns.show_notion_sheet(gone) is False
    assert ns._SHEET == {}


def test_a_second_call_raises_the_open_sheet_instead_of_opening_another(world):
    host = Host(MCP_CFG)
    seen = {}

    def second():
        first = ns._SHEET["sheet"]
        seen["result"] = ns.show_notion_sheet(host)
        seen["same"] = ns._SHEET["sheet"] is first
        first.reject()
    QTimer.singleShot(30, second)
    ns.show_notion_sheet(host)
    assert seen == {"result": False, "same": True}


# ── looks ────────────────────────────────────────────────────────────────────
STATES = ("intro", "intro_error", "waiting", "finishing", "connected", "connected_then", "connected_picker",
          "manual", "manual_error", "share")


def _build_state(make, name):
    run = Deferred()
    if name == "connected":
        sheet, _ = make(MCP_CFG, run=run)
        return sheet
    if name == "connected_then":
        sheet, _ = make(dict(MCP_CFG, notion_auto_send=True), run=run, then="Send this meeting")
        return sheet
    if name == "connected_picker":
        sheet, _ = make(OAUTH_CFG, run=run)
        sheet._fill_picker(PAGES)
        return sheet
    if name == "share":
        sheet, _ = make(run=run)
        sheet._on_no_page({"mode": "own", "token": "t", "refresh": "", "workspace": "", "dest": None, "pages": []})
        return sheet
    sheet, _ = make(run=run)
    if name == "intro_error":
        sheet._show_intro("That took too long. Nothing was connected.")
    elif name in ("waiting", "finishing"):
        sheet.connect_btn.click()
        if name == "finishing":
            sheet.flow.finishing.emit()
        sheet.progress.pin(0.3)
    elif name in ("manual", "manual_error"):
        sheet._show_manual("")
        if name == "manual_error":
            sheet.secret.edit.setText("ntn_1234567890abcdef")
            sheet._on_failed("rejected", notion_connect.MESSAGES["rejected"])
    return sheet


@pytest.mark.parametrize("state", STATES)
def test_every_visible_control_has_a_name(world, make, state):
    sheet = _build_state(make, state)
    controls = _visible_controls(sheet)
    assert controls or state in ("waiting", "finishing")
    for wd in controls:
        assert _name_of(wd), "%s: a %s without a name" % (state, type(wd).__name__)
    assert sheet.height() <= ns.MAX_H and sheet.width() == ns.W


def test_the_text_colours_are_readable_on_the_window_and_the_card():
    for token in ns.TEXT_ON_WINDOW:
        for stop in ns.WINDOW_STOPS:
            assert contrast(_rgb(token), _rgb(stop)) >= 4.5, (token, stop)
    # "muted" is only for the white card
    assert contrast(_rgb("muted"), _rgb("white")) >= 4.5
    assert "muted" not in ns.TEXT_ON_WINDOW
    assert contrast(_rgb("white"), _rgb("blue")) >= 4.5                  # the primary button
    assert contrast(_rgb("ok-tx"), _rgb("ok-sub")) >= 4.5                # the "Connected" chip
    assert contrast(_rgb("text"), _rgb("white")) >= 4.5                  # the card's labels


@pytest.mark.parametrize("state", STATES)
def test_every_label_is_readable_where_it_is_painted(world, make, state):
    """The real pixels: each visible label's colour against the background that is
    actually under it in the render, 4.5:1 at least."""
    import re
    sheet = _build_state(make, state)
    img = sheet.grab().toImage()
    checked = 0
    for lb in sheet.findChildren(QLabel):
        if not lb.isVisibleTo(sheet) or not lb.text().strip():
            continue
        m = re.search(r"color:\s*(#[0-9a-fA-F]{6})", lb.styleSheet())
        if not m:
            continue
        fg = QColor(m.group(1))
        top_left = lb.mapTo(sheet, QPoint(0, 0))
        bg = QColor(img.pixel(max(0, top_left.x()), max(0, top_left.y())))
        ratio = contrast((fg.red(), fg.green(), fg.blue()), (bg.red(), bg.green(), bg.blue()))
        assert ratio >= 4.5, "%s: %r is %.2f:1" % (state, lb.text()[:40], ratio)
        checked += 1
    assert checked >= 2


def _dark_palette():
    pal = QPalette()
    for role, col in ((QPalette.Window, "#202020"), (QPalette.WindowText, "#ffffff"), (QPalette.Base, "#2b2b2b"),
                      (QPalette.Text, "#ffffff"), (QPalette.Button, "#2d2d2d"), (QPalette.ButtonText, "#ffffff"),
                      (QPalette.PlaceholderText, "#9d9d9d"), (QPalette.Link, "#4cc2ff"),
                      (QPalette.ToolTipBase, "#2b2b2b"), (QPalette.ToolTipText, "#ffffff")):
        pal.setColor(role, QColor(col))
    return pal


def test_the_sheet_looks_the_same_on_a_dark_windows(world, make):
    """Every pixel is painted by the Gusan stylesheet and tokens, never the OS
    palette: a dark QApplication palette changes nothing (the old dialog went
    dark-on-dark exactly because it ignored the theme)."""
    app = QApplication.instance()
    old = app.palette()
    light = {s: _build_state(make, s).grab().toImage() for s in STATES}
    app.setPalette(_dark_palette())
    try:
        dark = {s: _build_state(make, s).grab().toImage() for s in STATES}
    finally:
        app.setPalette(old)
    for s in STATES:
        assert light[s].size() == dark[s].size(), s
        assert light[s] == dark[s], "%s differs under a dark palette" % s


def test_manual_busy_state_looks_the_same_on_a_dark_windows(world, make):
    app = QApplication.instance()
    old = app.palette()

    def busy():
        run = Deferred()
        sheet, _ = _manual(make, run=run)
        sheet.secret.edit.setText("ntn_1234567890abcdef")
        sheet.manual_btn.click()
        return sheet.grab().toImage()
    light = busy()
    app.setPalette(_dark_palette())
    try:
        dark = busy()
    finally:
        app.setPalette(old)
    assert light == dark


def test_the_progress_line_moves_only_while_it_is_on_screen(world, make):
    sheet, _host = make(run=Deferred())
    line = sheet.progress
    assert not line._timer.isActive()                          # on a page that is not showing
    sheet.connect_btn.click()
    assert line._timer.isActive()
    pos = {line.position()}
    for t in (0.0, 0.4, 0.8, 1.2):
        line._t0 -= 0.4
        pos.add(round(line.position(), 3))
    assert all(0.0 <= p <= 1.0 for p in pos) and len(pos) > 2
    sheet.cancel_btn.click()
    assert not line._timer.isActive()


def test_the_mark_is_a_drawn_tile_not_a_file(qapp):
    for size in (16, 48):
        pm = ns.notion_pixmap(size)
        assert pm.width() >= size and pm.height() >= size and not pm.isNull()
    lb = ns.mark_label(16)
    assert lb.accessibleName() == "Notion" and lb.width() == 16 and lb.height() == 16


# ══ review fixes ═════════════════════════════════════════════════════════════
# ── Privacy mode: opening Manage must not go online by itself ────────────────
def test_opening_manage_in_privacy_mode_goes_nowhere_until_asked(world, make):
    run = Deferred()
    sheet, host = make(dict(OAUTH_CFG, privacy_mode=True), run=run)
    assert sheet.page_name() == "connected"
    assert not run.jobs and world.asked == []                       # no search, no refresh, no question
    assert sheet.dest_hint.text() == ns.DEST_HINT_PAUSED == "Paused while Privacy mode is on. Refresh to load your pages."
    assert sheet.picker.itemText(0) == "Gusan meetings (private page)"      # what is saved still shows
    world.answer = False
    sheet.refresh_btn.click()
    assert len(world.asked) == 1 and not run.jobs                   # asked once, said no: still offline
    assert world.asked[0][0] == "Privacy mode is on" and world.asked[0][4] == "incognito"
    world.answer = True
    sheet.refresh_btn.click()
    assert len(world.asked) == 2 and [j.fn.__name__ for j in run.jobs] == ["_pages_work"]
    run.finish(0)
    assert sheet.dest_hint.text() == "To add pages, share them with Gusan in Notion."     # the paused line is gone
    assert sheet.picker.count() == 1 + len(PAGES)
    sheet.refresh_btn.click()                                       # a yes covers the rest of this sheet
    assert len(world.asked) == 2 and len(run.jobs) == 2


def test_refresh_without_privacy_mode_never_asks(world, make):
    run = Deferred()
    sheet, _host = make(OAUTH_CFG, run=run)
    sheet.refresh_btn.click()
    assert world.asked == [] and len(run.jobs) == 2


def test_a_paused_sheet_does_not_search_on_typing(world, make):
    run = Deferred()
    sheet, _host = make(dict(OAUTH_CFG, privacy_mode=True), run=run)
    sheet.search.setText("road")
    sheet._search_timer.stop()
    sheet._search_fire()
    assert not run.jobs and world.asked == []


# ── a name from Notion is text, never markup ─────────────────────────────────
def test_a_workspace_name_is_drawn_as_plain_text(world, make, tmp_path):
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QImage
    img = QImage(8, 8, QImage.Format_RGB32)
    img.fill(QColor("#ff0000"))
    png = tmp_path / "x.png"
    assert img.save(str(png))
    hostile = '<img src="%s" width="40" height="40">' % png.as_uri()
    sheet, _host = make(dict(MCP_CFG, notion_workspace_name=hostile))
    lb = sheet.name_label
    assert lb.textFormat() == Qt.PlainText
    assert lb.full_text() == hostile                                 # shown as typed, tags and all

    def reds(label):
        img = label.grab().toImage()
        return sum(1 for x in range(img.width()) for y in range(img.height())
                   if QColor(img.pixel(x, y)).red() > 200 and QColor(img.pixel(x, y)).green() < 60)
    control = QLabel(hostile)                                        # the stock label does load it
    control.resize(200, 60)
    assert reds(control) > 100
    assert reds(lb) == 0                                             # ours loads nothing


# ── Disconnect that could not give the grant back ────────────────────────────
@pytest.mark.parametrize("cfg", [MCP_CFG, OAUTH_CFG])
def test_a_disconnect_that_could_not_revoke_says_so(world, make, cfg):
    world.revoke_ok = False
    sheet, host = make(cfg)
    sheet.disconnect_link.click()
    assert host.cfg()["notion_api_key"] == ""                         # forgotten here, as always
    tips = [e for e in host.events if e[:2] == ("call", "show_tray_hint")]
    assert tips == [("call", "show_tray_hint", "Notion",
                     "Removed from Gusan. Also remove it in Notion under Settings > Connections.")]


def test_a_disconnect_that_did_revoke_says_nothing(world, make):
    sheet, host = make(MCP_CFG)
    sheet.disconnect_link.click()
    assert world.revoked == ["tok-1"]
    assert not [e for e in host.events if e[:2] == ("call", "show_tray_hint")]


def test_a_pasted_secret_has_nothing_to_give_back_so_nothing_is_said(world, make):
    world.revoke_ok = False
    sheet, host = make(OWN_CFG)
    sheet.disconnect_link.click()
    assert not [e for e in host.events if e[:2] == ("call", "show_tray_hint")]


# ── "Search pages" ───────────────────────────────────────────────────────────
def _many(n):
    return [{"id": "%08x-1111-2222-3333-%012x" % (i, i), "title": "Page %d" % i, "emoji": "", "url": "",
             "top": True} for i in range(1, n + 1)]


def test_typing_in_search_sends_the_text_to_notion_after_a_pause(world, make, monkeypatch):
    queries = []

    def search(token, query="", limit=50):
        queries.append((query, limit))
        return [p for p in world.pages if query.lower() in p["title"].lower()]
    monkeypatch.setattr(notion_connect, "search_pages", search)
    world.pages = _many(ns.PAGE_LIMIT)
    sheet, _host = make(OAUTH_CFG)
    assert queries == [("", ns.PAGE_LIMIT)]
    assert sheet.search_row.isVisibleTo(sheet)                        # a full list: the field is there
    sheet.search.setText("Page 7")
    assert sheet._search_timer.isActive() and len(queries) == 1       # waits for a pause
    sheet._search_timer.stop()
    sheet._search_fire()
    assert queries[-1] == ("Page 7", ns.PAGE_LIMIT)
    texts = [sheet.picker.itemText(i) for i in range(sheet.picker.count())]
    assert "Page 7" in texts and "Page 8" not in texts
    assert sheet.search_row.isVisibleTo(sheet)                        # a search is on: the field stays
    sheet.refresh_btn.click()
    assert queries[-1] == ("Page 7", ns.PAGE_LIMIT)                   # Refresh keeps the text


def test_the_newest_search_wins(world, make, monkeypatch):
    run = Deferred()
    world.pages = _many(ns.PAGE_LIMIT)
    sheet, _host = make(OAUTH_CFG, run=run)
    sheet.search.setText("a")
    sheet._search_timer.stop()
    sheet._search_fire()
    sheet.search.setText("ab")
    sheet._search_timer.stop()
    sheet._search_fire()
    assert [j.args[3] for j in run.jobs] == ["", "a", "ab"]
    seen = []
    sheet.flow.pages_loaded.connect(lambda pages, code: seen.append(len(pages)))
    run.finish(0)
    run.finish(1)
    assert seen == []                                                 # older answers are dropped
    run.finish(2)
    assert seen == [ns.PAGE_LIMIT]


def test_the_search_field_only_shows_when_the_list_is_full_or_a_search_is_on(world, make):
    sheet, _host = make(OAUTH_CFG)                                    # three pages: nothing to search for
    assert not sheet.search_row.isVisibleTo(sheet)
    world.pages = _many(ns.PAGE_LIMIT - 1)
    sheet.refresh_btn.click()
    assert not sheet.search_row.isVisibleTo(sheet)
    world.pages = _many(ns.PAGE_LIMIT)
    sheet.refresh_btn.click()
    assert sheet.search_row.isVisibleTo(sheet)
    assert sheet.search.placeholderText() == "Search pages" and sheet.search.accessibleName() == "Search pages"
    assert sheet.height() <= ns.MAX_H
    for wd in _visible_controls(sheet):
        assert _name_of(wd)


def test_search_matches_the_hub_by_its_title_and_always_keeps_the_saved_page(world, make, monkeypatch):
    world.pages = _many(ns.PAGE_LIMIT)
    sheet, _host = make(OAUTH_CFG)
    monkeypatch.setattr(notion_connect, "search_pages", lambda token, query="", limit=50: [])
    sheet.search.setText("zzz")
    sheet._search_timer.stop()
    sheet._search_fire()
    # nothing matches, and the hub's name does not either: only the saved destination is left, still selected
    assert [sheet.picker.itemText(i) for i in range(sheet.picker.count())] == ["Gusan meetings"]
    assert sheet.picker.currentIndex() == 0
    sheet.search.setText("gusan")
    sheet._search_timer.stop()
    sheet._search_fire()
    assert sheet.picker.itemText(0) == "Gusan meetings (private page)"      # the hub matches its own name
    assert sheet.picker.currentIndex() == 0


def test_enter_in_search_searches_now_and_does_not_press_done(world, make, monkeypatch):
    from PySide6.QtTest import QTest
    queries = []
    monkeypatch.setattr(notion_connect, "search_pages",
                        lambda token, query="", limit=50: queries.append(query) or list(_many(ns.PAGE_LIMIT)))
    sheet, _host = make(OAUTH_CFG)
    sheet.search.setText("road")
    sheet._search_timer.stop()
    sheet.search.setFocus()
    QTest.keyClick(sheet.search, Qt.Key_Return)
    assert queries[-1] == "road"
    assert sheet.isVisible() and sheet.result_sent is False           # the sheet did not close


# ── the ordinary ends of connecting are not logged as failed tasks ───────────
def test_cancel_returns_an_answer_instead_of_raising(world, make):
    sheet, _host = make(connect=fake_connect(world, error=notion_connect.ConnectCancelled()))
    ev = threading.Event()
    res = sheet.flow._connect_work(ev, {})
    assert isinstance(res["error"], notion_connect.ConnectCancelled)
    for code in ("denied", "timeout", "offline", "port_busy", "expired", "server", "browser", "full", "other"):
        sheet.flow._connect_fn = fake_connect(world, error=ConnectError(code))
        assert sheet.flow._connect_work(ev, {})["error"].code == code


def test_the_real_connect_is_what_the_button_calls_when_nothing_replaces_it(world, make, monkeypatch):
    seen = {}

    def real(cfg=None, open_browser=None, timeout=0, cancel=None, on_url=None, on_code=None, persist=None):
        seen.update(cfg=cfg, cancel=cancel, has_url=callable(on_url), has_code=callable(on_code),
                    has_persist=callable(persist), browser=open_browser)
        return connect_result()
    monkeypatch.setattr(notion_connect, "connect_mcp", real)
    sheet, host = make()
    sheet.flow._connect_fn = None
    sheet.connect_btn.click()
    assert sheet.page_name() == "connected"
    assert seen["has_url"] and seen["has_code"] and seen["has_persist"] and isinstance(seen["cancel"], threading.Event)
    seen["browser"]("https://mcp.notion.com/authorize?x=1")               # the sheet opens the browser, not the core
    assert world.opened[-1] == "https://mcp.notion.com/authorize?x=1"


def test_a_real_fault_is_still_raised_and_logged(world, make, caplog):
    import api_keys
    sheet, _host = make(connect=fake_connect(world, error=RuntimeError("a bug")))
    done = threading.Event()
    with caplog.at_level(logging.WARNING):
        t = api_keys.run_in_background(sheet.flow._connect_work, threading.Event(), {}, on_error=lambda e: done.set())
        t.join(10)
    assert done.is_set()
    assert any("Background task" in r.getMessage() for r in caplog.records)


def test_pressing_cancel_logs_no_failed_task(world, make, caplog):
    """The real worker thread (api_keys.run_in_background) with a connect that ends in a cancel:
    nothing is logged as a failed background task, and the answer reaches the sheet."""
    import api_keys
    sheet, host = make(connect=fake_connect(world, error=notion_connect.ConnectCancelled()))
    answers = []
    with caplog.at_level(logging.DEBUG):
        t = api_keys.run_in_background(sheet.flow._connect_work, threading.Event(), {}, on_done=answers.append)
        t.join(10)
    assert len(answers) == 1 and isinstance(answers[0]["error"], notion_connect.ConnectCancelled)
    assert not [r for r in caplog.records if "Background task" in r.getMessage() or r.exc_info]


def test_the_sheet_shows_a_returned_failure_like_a_raised_one(world, make):
    for code in ("denied", "timeout"):
        sheet, host = make(connect=fake_connect(world, error=ConnectError(code)))
        sheet.connect_btn.click()
        assert sheet.intro_message.text() == notion_connect.MESSAGES[code]
        assert host.tracked("notion_connect_result") == [{"mode": "mcp", "ok": False, "reason": code}]
    run = Deferred()
    sheet, host = make(run=run, connect=fake_connect(world, error=notion_connect.ConnectCancelled()))
    sheet.connect_btn.click()
    sheet.cancel_btn.click()
    run.finish(0)                                                    # the late "cancelled" answer changes nothing
    assert sheet.page_name() == "intro" and not sheet.intro_message.isVisibleTo(sheet)
    assert len(host.tracked("notion_connect_result")) == 1


# ── a refreshed token is kept even when its answer is late ───────────────────
def test_a_token_refreshed_by_the_picker_is_saved_even_if_the_sheet_closed_first(world, make):
    run = Deferred()
    world.search_outcomes = [NotionAuthError("401")]
    world.refresh_result = dict(TOKEN, access_token="tok-9", refresh_token="ref-9")
    sheet, host = make(OAUTH_CFG, run=run)
    sheet.flow.shutdown()                                            # the sheet closes: late answers are dropped...
    run.finish(0)                                                    # ...but Notion already rotated the old token
    assert world.refreshed == ["ref-1"]
    assert host.cfg()["notion_api_key"] == "tok-9" and host.cfg()["notion_refresh_token"] == "ref-9"
    assert host.cfg()["notion_reconnect"] is False


def test_the_late_pair_is_not_saved_after_a_reconnect_or_a_disconnect(world, make):
    run = Deferred()
    world.search_outcomes = [NotionAuthError("401")]
    world.refresh_result = dict(TOKEN, access_token="tok-9", refresh_token="ref-9")
    sheet, host = make(OAUTH_CFG, run=run)
    host.cfg_["notion_refresh_token"] = "ref-from-a-new-connection"
    sheet.flow.shutdown()
    run.finish(0)
    assert host.cfg()["notion_api_key"] == "tok-1" and host.cfg()["notion_refresh_token"] == "ref-from-a-new-connection"


# ── the picker's closed label ────────────────────────────────────────────────
def _dark_columns(img, x0, x1, y0, y1):
    """Columns in [x0, x1) holding a dark (text) pixel between y0 and y1."""
    out = []
    for x in range(x0, x1):
        if any(QColor(img.pixel(x, y)).lightness() < 140 for y in range(y0, y1)):
            out.append(x)
    return out


def test_a_long_page_title_ends_in_an_ellipsis_in_the_closed_picker(world, make):
    from PySide6.QtWidgets import QStyle, QStyleOptionComboBox
    long_title = "Roadmap 2027 planning with the whole leadership team"
    cfg = dict(OAUTH_CFG, notion_parent_page=PAGES[2]["id"], notion_parent_title=long_title)
    sheet, _host = make(cfg)
    picker = sheet.picker
    assert isinstance(picker, ns.ElidedComboBox)
    assert picker.currentText().startswith("Roadmap 2027")
    assert picker.itemText(picker.currentIndex()) == picker.currentText()       # only the painting changes
    opt = QStyleOptionComboBox()
    picker.initStyleOption(opt)
    field = picker.style().subControlRect(QStyle.CC_ComboBox, opt, QStyle.SC_ComboBoxEditField, picker)
    img = picker.grab().toImage()
    dpr = img.devicePixelRatio()
    right = int(round((field.right() + 1) * dpr))
    h = img.height()
    # Not a single letter is cut at the edge of the field: nothing dark in its last pixels...
    assert not _dark_columns(img, right - 3, right, int(h * 0.3), int(h * 0.7))
    # ...and the text itself did reach near the edge (so it was elided, not shortened to nothing).
    assert _dark_columns(img, right - int(60 * dpr), right - 3, int(h * 0.3), int(h * 0.7))


def test_a_short_title_is_unchanged(world, make):
    sheet, _host = make(OAUTH_CFG)
    assert isinstance(sheet.picker, ns.ElidedComboBox)
    assert sheet.picker.currentText() == "Gusan meetings (private page)"


# ── the refresh button's keyboard ring ───────────────────────────────────────
def test_the_framed_refresh_button_shows_its_ring_for_the_keyboard(world, make):
    sheet, _host = make(OAUTH_CFG)
    btn = sheet.refresh_btn
    before = btn.grab().toImage()
    theme.set_prop(btn, "focusVisible", True, children=False)
    QApplication.processEvents()
    after = btn.grab().toImage()
    assert before != after
    ring = QColor(after.pixel(0, after.height() // 2))
    assert (ring.red(), ring.green(), ring.blue()) == _rgb("blue-l")
    assert QColor(after.pixel(1, after.height() // 2)).name() == ring.name()      # two pixels wide


# ── links: the ring has room, the text lines up ──────────────────────────────
def _ink_columns(img, x0=0):
    """x of every column from x0 holding a dark, opaque pixel (a letter)."""
    out = []
    for x in range(x0, img.width()):
        for y in range(img.height()):
            c = img.pixelColor(x, y)
            if c.alpha() > 120 and c.lightness() < 150:
                out.append(x)
                break
    return out


def test_a_focused_link_keeps_its_text_where_it_was_and_its_ring_clear_of_the_letters(world, make):
    sheet, _host = make()
    link = sheet.own_link
    plain = link.grab().toImage()
    theme.set_prop(link, "focusVisible", True, children=False)
    QApplication.processEvents()
    ring = link.grab().toImage()
    dpr = ring.devicePixelRatio()
    h = ring.height()
    # the 2 px ring is drawn at the edge...
    assert ring.pixelColor(0, h // 2).name() == QColor(theme.qcolor("blue-l")).name()
    assert ring.pixelColor(int(dpr), h // 2).name() == QColor(theme.qcolor("blue-l")).name()
    # ...and the first letter starts at least 3 px past it
    ring_edge = int(2 * dpr)
    first = _ink_columns(ring, ring_edge)[0]
    assert first - ring_edge >= int(3 * dpr)
    # the letters did not move when the ring appeared
    assert abs(_ink_columns(plain, ring_edge)[0] - first) <= 1
    # ...and the last letter is clear of the ring on the right as well
    last = _ink_columns(ring, ring_edge)[-1]
    assert (ring.width() - 1 - int(2 * dpr)) - last >= int(3 * dpr) - 1


def test_the_links_text_lines_up_with_the_text_above_it(world, make):
    sheet, _host = make()
    caption = [lb for lb in sheet.findChildren(QLabel) if lb.text() == ns.CAPTION][0]
    cx = caption.mapTo(sheet, QPoint(0, 0)).x()
    lx = sheet.own_link.mapTo(sheet, QPoint(0, 0)).x() + ns.RING
    assert lx == cx
    assert sheet.own_link.mapTo(sheet, QPoint(0, 0)).x() >= sheet.stack.mapTo(sheet, QPoint(0, 0)).x()    # not clipped
    connected, _ = make(MCP_CFG)
    card_left = connected.disconnect_link.parentWidget().mapTo(connected, QPoint(0, 0)).x() + ns.INSET
    assert connected.disconnect_link.mapTo(connected, QPoint(0, 0)).x() + ns.RING == card_left


# ── where the keyboard starts ────────────────────────────────────────────────
def test_the_keyboard_starts_on_the_main_control_of_each_screen(world, make):
    for cfg, page, attr in ((None, "intro", "connect_btn"), (MCP_CFG, "connected", "done_btn"),
                            (OAUTH_CFG, "connected", "done_btn"),
                            (dict(MCP_CFG, notion_reconnect=True), "intro", "connect_btn")):
        sheet, _host = make(cfg)
        sheet.activateWindow()
        QApplication.processEvents()
        QApplication.processEvents()
        assert sheet.page_name() == page
        assert sheet.focusWidget() is getattr(sheet, attr), (page, sheet.focusWidget())
    assert sheet.scroll.focusPolicy() == Qt.NoFocus                   # not a phantom Tab stop


def test_the_secret_field_has_the_keyboard_when_the_manual_screen_opens(world, make, monkeypatch):
    monkeypatch.setattr(notion_connect, "mcp_available", lambda: False)
    sheet, _host = make()
    QApplication.processEvents()
    QApplication.processEvents()
    assert sheet.page_name() == "manual" and sheet.focusWidget() is sheet.secret.edit


# ── the sheet stays on the desktop when a screen grows ───────────────────────
@pytest.mark.parametrize("size", [(1093, 566), (910, 464)])
def test_a_taller_screen_is_pulled_back_above_the_taskbar(world, make, monkeypatch, size):
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "fit_on_screen", _REAL_FIT)
    avail = QRect(0, 0, *size)                                        # 1366x768 at 125% / 150%, minus the taskbar
    sheet, _host = make(show=False)
    monkeypatch.setattr(sheet, "screen", lambda: SimpleNamespace(availableGeometry=lambda: avail))
    sheet.show()
    QApplication.processEvents()
    assert sheet.frameGeometry().bottom() <= avail.bottom()
    sheet.own_link.click()                                            # the secret screen is taller
    QApplication.processEvents()
    assert sheet.page_name() == "manual"
    assert avail.top() <= sheet.frameGeometry().top() and sheet.frameGeometry().bottom() <= avail.bottom()
    sheet._show_manual("Notion didn't accept that secret. Copy it again from your integration.")
    assert avail.top() <= sheet.frameGeometry().top() and sheet.frameGeometry().bottom() <= avail.bottom()
    assert sheet.height() <= avail.height() - 24
    if sheet.height() < sheet.stack.height() + ns.TOP + ns.BOTTOM:    # too tall for this screen: it scrolls
        assert sheet.scroll.verticalScrollBar().maximum() > 0


def test_a_screen_that_fits_is_left_alone(world, make, monkeypatch):
    import ui.winfit
    monkeypatch.setattr(ui.winfit, "fit_on_screen", _REAL_FIT)
    sheet, _host = make(show=False)
    monkeypatch.setattr(sheet, "screen", lambda: SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 1920, 1040)))
    sheet.show()
    QApplication.processEvents()
    h = sheet.height()
    sheet.own_link.click()
    assert sheet.height() >= h and sheet.height() <= ns.MAX_H


# ── the privacy question, the same as everywhere ─────────────────────────────
def test_the_privacy_question_is_the_apps_own(world, make):
    from ui import privacy_prompt
    assert ns.PRIVACY_TITLE == privacy_prompt.TITLE
    assert ns.PRIVACY_BODY == "This goes online. Connect anyway?"
    sheet, _host = make({"privacy_mode": True})
    sheet.connect_btn.click()
    assert world.asked[0][4] == "incognito"


def test_the_copy_of_the_review(world, make):
    assert notion_connect.MESSAGES["port_busy"] == (
        "Couldn't start the connection on this computer. Close other Gusan windows and try again.")
    assert ns.OWN_LINK == "Use your own Notion secret instead"
    assert ns.DEST_HINT == "To add pages, share them with Gusan in Notion."


def test_a_pasted_secret_that_can_see_a_full_list_gets_the_search_field_at_once(world, make):
    world.pages = _many(ns.PAGE_LIMIT)
    sheet, host = _manual(make)
    sheet.secret.edit.setText("ntn_abc")
    sheet.manual_btn.click()
    assert sheet.page_name() == "connected" and sheet.search_row.isVisibleTo(sheet)
    world.pages = list(PAGES)
    sheet2, _ = _manual(make)
    sheet2.secret.edit.setText("ntn_abc")
    sheet2.manual_btn.click()
    assert sheet2.page_name() == "connected" and not sheet2.search_row.isVisibleTo(sheet2)
