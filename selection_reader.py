"""The text the person has selected on screen, read when the Smart Actions shortcut is pressed
(docs/redesign/selected-text-spec.md).

* Windows: UI Automation asks the focused app for its selection - no keys sent, the clipboard untouched. An app that
  does not answer (no text pattern) gets Ctrl+C instead, the clipboard is read and the person's text put back. Never
  Ctrl+C into a terminal or an editor with a terminal (there it stops the running program).
* macOS: Cmd+C (safe in every app, Terminal included), read, put the person's text back. A password box copies
  nothing, so it is never read.

Everything here runs on a worker thread and never raises: no selection is an empty Selection.
"""
import ctypes
import ctypes.util
import logging
import os
import sys
import threading
import time
from dataclasses import dataclass

logger = logging.getLogger(__name__)

MAX_CHARS = 20000              # longer selections are cut (the pill says so)
KEYS_UP_WAIT_S = 0.8           # the shortcut's keys must be up before a copy chord (Alt+Ctrl+C is another shortcut)
COPY_WAIT_S = 1.0              # how long the app may take to put its selection on the clipboard (Google Docs: ~0.5 s)

# Windows: no copy chord into these (window classes / programs where Ctrl+C can stop a running program).
_TERMINAL_CLASSES = {"ConsoleWindowClass", "CASCADIA_HOSTING_WINDOW_CLASS", "mintty", "PuTTY", "VirtualConsoleClass",
                     "org.wezfurlong.wezterm", "Alacritty", "TMobaXtermForm"}
# Browsers: their page reports itself read-only to UI Automation even where you can type (Google Docs draws its own
# text), so in a browser "can the cursor take typing" is not known - the result is pasted and stays on the clipboard.
_BROWSERS = {"chrome.exe", "msedge.exe", "firefox.exe", "brave.exe", "opera.exe", "vivaldi.exe", "arc.exe",
             "iexplore.exe", "chromium.exe", "thorium.exe", "librewolf.exe", "zen.exe"}
_TERMINAL_PROGRAMS = {"windowsterminal.exe", "cmd.exe", "powershell.exe", "pwsh.exe", "conhost.exe",
                      "wezterm-gui.exe", "alacritty.exe", "mintty.exe", "putty.exe", "kitty.exe", "mobaxterm.exe",
                      "code.exe", "code - insiders.exe", "cursor.exe", "windsurf.exe", "idea64.exe", "pycharm64.exe",
                      "rider64.exe", "webstorm64.exe", "goland64.exe", "clion64.exe"}


@dataclass
class Selection:
    text: str = ""
    source: str = ""            # "uia" | "copy" | "" (nothing read)
    editable: object = None     # True / False / None (not known): can the selected text be changed where it is
    cut: bool = False           # longer than MAX_CHARS: cut to it

    def __bool__(self):
        return bool(self.text.strip())


def _finish(text, source, editable=None):
    text = str(text or "")
    if not text.strip():
        return Selection()
    cut = len(text) > MAX_CHARS
    return Selection(text[:MAX_CHARS] if cut else text, source, editable, cut)


# ── Windows: UI Automation through plain ctypes (no extra package) ─────────────────────────────
class _GUID(ctypes.Structure):
    _fields_ = [("Data1", ctypes.c_ulong), ("Data2", ctypes.c_ushort), ("Data3", ctypes.c_ushort),
                ("Data4", ctypes.c_ubyte * 8)]

    @classmethod
    def parse(cls, text):
        import uuid
        u = uuid.UUID(text)
        g = cls()
        g.Data1, g.Data2, g.Data3 = u.fields[0], u.fields[1], u.fields[2]
        for i, b in enumerate(u.bytes[8:]):
            g.Data4[i] = b
        return g


_CLSID_CUIAutomation = "ff48dba4-60ef-4201-aa87-54103eef594e"
_IID_IUIAutomation = "30cbe57d-d9d0-452a-ab13-7ac5ac4825ee"
_IID_IUIAutomationTextPattern = "32eba289-3583-42c9-9c59-3b6d9a1e9b6a"
_UIA_TextPatternId = 10014
_UIA_ValueIsReadOnlyPropertyId = 30046
_VT_BOOL = 11

# vtable slots (UIAutomationClient.h; 0-2 are IUnknown)
_AUTOMATION_GET_FOCUSED = 8
_ELEMENT_GET_PROPERTY = 10
_ELEMENT_GET_PATTERN_AS = 14
_ELEMENT_IS_PASSWORD = 35
_TEXTPATTERN_GET_SELECTION = 5
_RANGEARRAY_LENGTH = 3
_RANGEARRAY_ELEMENT = 4
_RANGE_GET_TEXT = 12


def _method(ptr, index, restype, *argtypes):
    """Slot ``index`` of a COM object's vtable as a callable (HRESULTs come back as plain numbers: 0 is S_OK)."""
    vtable = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return lambda *args: proto(vtable[index])(ptr, *args)


def _release(ptr):
    if ptr:
        try:
            _method(ptr, 2, ctypes.c_ulong)()
        except Exception:
            pass


class _UIA:
    """One UI Automation client for the calling thread (COM is initialised and released around it)."""

    def __enter__(self):
        ole = ctypes.windll.ole32
        self._inited = ole.CoInitializeEx(None, 0x0) in (0, 1)        # COINIT_MULTITHREADED (S_OK / S_FALSE)
        self.ptr = ctypes.c_void_p()
        clsid, iid = _GUID.parse(_CLSID_CUIAutomation), _GUID.parse(_IID_IUIAutomation)
        hr = ole.CoCreateInstance(ctypes.byref(clsid), None, 1, ctypes.byref(iid), ctypes.byref(self.ptr))
        if hr != 0 or not self.ptr:
            self.ptr = None
        return self

    def __exit__(self, *exc):
        _release(self.ptr)
        if self._inited:
            ctypes.windll.ole32.CoUninitialize()
        return False

    def focused(self):
        el = ctypes.c_void_p()
        if not self.ptr:
            return None
        hr = _method(self.ptr, _AUTOMATION_GET_FOCUSED, ctypes.c_long, ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(el))
        return el if hr == 0 and el else None


def _is_password(el):
    flag = ctypes.c_int(0)
    try:
        _method(el, _ELEMENT_IS_PASSWORD, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(ctypes.byref(flag))
    except OSError:
        return False
    return bool(flag.value)


def _read_only(el):
    """True / False from the element's Value pattern, None when it does not say."""
    var = (ctypes.c_ubyte * 24)()
    try:
        hr = _method(el, _ELEMENT_GET_PROPERTY, ctypes.c_long, ctypes.c_int, ctypes.c_void_p)(
            _UIA_ValueIsReadOnlyPropertyId, ctypes.cast(var, ctypes.c_void_p))
    except OSError:
        return None
    if hr != 0:
        return None
    vt = int.from_bytes(bytes(var[0:2]), "little")
    value = None
    if vt == _VT_BOOL:
        value = int.from_bytes(bytes(var[8:10]), "little", signed=True) != 0
    try:
        ctypes.windll.oleaut32.VariantClear(ctypes.cast(var, ctypes.c_void_p))
    except Exception:
        pass
    return value


def _uia_selection():
    """(text, has_text_pattern, read_only, password) of the focused element; ("", False, None, False) when UI
    Automation cannot be reached."""
    with _UIA() as uia:
        el = uia.focused()
        if el is None:
            return "", False, None, False
        try:
            if _is_password(el):
                return "", True, None, True
            read_only = _read_only(el)
            pattern = ctypes.c_void_p()
            iid = _GUID.parse(_IID_IUIAutomationTextPattern)
            hr = _method(el, _ELEMENT_GET_PATTERN_AS, ctypes.c_long, ctypes.c_int, ctypes.c_void_p,
                         ctypes.POINTER(ctypes.c_void_p))(_UIA_TextPatternId, ctypes.byref(iid), ctypes.byref(pattern))
            if hr != 0 or not pattern:
                return "", False, read_only, False
            try:
                ranges = ctypes.c_void_p()
                hr = _method(pattern, _TEXTPATTERN_GET_SELECTION, ctypes.c_long,
                             ctypes.POINTER(ctypes.c_void_p))(ctypes.byref(ranges))
                if hr != 0 or not ranges:
                    return "", True, read_only, False
                try:
                    n = ctypes.c_int(0)
                    _method(ranges, _RANGEARRAY_LENGTH, ctypes.c_long, ctypes.POINTER(ctypes.c_int))(ctypes.byref(n))
                    parts = []
                    for i in range(min(n.value, 50)):
                        rng = ctypes.c_void_p()
                        if _method(ranges, _RANGEARRAY_ELEMENT, ctypes.c_long, ctypes.c_int,
                                   ctypes.POINTER(ctypes.c_void_p))(i, ctypes.byref(rng)) != 0 or not rng:
                            continue
                        try:
                            bstr = ctypes.c_void_p()
                            if _method(rng, _RANGE_GET_TEXT, ctypes.c_long, ctypes.c_int,
                                       ctypes.POINTER(ctypes.c_void_p))(MAX_CHARS + 1, ctypes.byref(bstr)) == 0 and bstr:
                                parts.append(ctypes.wstring_at(bstr.value))
                                ctypes.windll.oleaut32.SysFreeString(bstr)
                        finally:
                            _release(rng)
                    return "\n".join(p for p in parts if p), True, read_only, False
                finally:
                    _release(ranges)
            finally:
                _release(pattern)
        finally:
            _release(el)


def _foreground_window():
    """(window class, program file name in lower case) of the window in front; (None, None) when there is none or it
    can't be read."""
    try:
        user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
        hwnd = user32.GetForegroundWindow()
        if not hwnd:
            return None, None
        buf = ctypes.create_unicode_buffer(256)
        user32.GetClassNameW(hwnd, buf, 256)
        pid = ctypes.c_ulong(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        name = ""
        handle = kernel32.OpenProcess(0x1000, False, pid.value)          # PROCESS_QUERY_LIMITED_INFORMATION
        if handle:
            try:
                size = ctypes.c_ulong(1024)
                path = ctypes.create_unicode_buffer(1024)
                if kernel32.QueryFullProcessImageNameW(handle, 0, path, ctypes.byref(size)):
                    name = path.value.replace("/", "\\").rsplit("\\", 1)[-1].lower()
            finally:
                kernel32.CloseHandle(handle)
        return buf.value, name
    except Exception:
        return None, None


def _foreground_is_terminal():
    """True when the window in front is a terminal, or a program with one built in (no copy chord there)."""
    cls, name = _foreground_window()
    if cls is None:
        return True                             # nothing in front, or unreadable: send nothing
    return cls in _TERMINAL_CLASSES or name in _TERMINAL_PROGRAMS


# ── the copy-chord fallback (both systems) ────────────────────────────────────────────────────
def _keys_up(max_s=KEYS_UP_WAIT_S):
    """Wait until no modifier is held (the Smart Actions shortcut's own keys)."""
    deadline = time.monotonic() + max_s
    if sys.platform == "win32":
        try:
            state = ctypes.windll.user32.GetAsyncKeyState
            keys = (0x10, 0x11, 0x12, 0x5B, 0x5C)                        # Shift Ctrl Alt Win Win
            while any(state(k) & 0x8000 for k in keys):
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.015)
            return True
        except Exception:
            time.sleep(0.25)
            return True
    if sys.platform == "darwin":
        try:
            quartz = ctypes.cdll.LoadLibrary("/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")
            quartz.CGEventSourceFlagsState.restype = ctypes.c_uint64
            quartz.CGEventSourceFlagsState.argtypes = [ctypes.c_int32]
            mods = 0x20000 | 0x40000 | 0x80000 | 0x100000                    # shift ctrl option command
            while quartz.CGEventSourceFlagsState(0) & mods:
                if time.monotonic() > deadline:
                    return False
                time.sleep(0.015)
            return True
        except Exception:
            time.sleep(0.25)
            return True
    time.sleep(0.25)
    return True


def _change_count():
    """A number that changes whenever anything is put on the clipboard, or None."""
    try:
        if sys.platform == "win32":
            return int(ctypes.windll.user32.GetClipboardSequenceNumber())
        if sys.platform == "darwin":
            objc = ctypes.cdll.LoadLibrary(ctypes.util.find_library("objc"))
            ctypes.cdll.LoadLibrary("/System/Library/Frameworks/AppKit.framework/AppKit")
            objc.objc_getClass.restype = ctypes.c_void_p
            objc.objc_getClass.argtypes = [ctypes.c_char_p]
            objc.sel_registerName.restype = ctypes.c_void_p
            objc.sel_registerName.argtypes = [ctypes.c_char_p]
            send_id = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
            send_long = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p)(("objc_msgSend", objc))
            board = send_id(objc.objc_getClass(b"NSPasteboard"), objc.sel_registerName(b"generalPasteboard"))
            return int(send_long(board, objc.sel_registerName(b"changeCount")))
    except Exception:
        logger.debug("clipboard change count unavailable", exc_info=True)
    return None


def _send_copy(kbd=None):
    from pynput.keyboard import Controller, Key
    kbd = kbd or Controller()
    mod = Key.cmd if sys.platform == "darwin" else Key.ctrl
    kbd.press(mod)
    kbd.press("c")
    kbd.release("c")
    kbd.release(mod)


def _copy_selection(kbd=None, clip=None, change_count=None, send_copy=None, wait_s=COPY_WAIT_S):
    """Send the copy chord, read what it put on the clipboard, put the person's text back. "" when nothing was
    selected (the clipboard did not change)."""
    if clip is None:
        import pyperclip as clip
    change_count = change_count or _change_count
    send_copy = send_copy or _send_copy
    try:
        before = clip.paste()
    except Exception:
        before = None
    count = change_count()
    sentinel = None
    if count is None:
        # No change counter: a marker on the clipboard tells "nothing was copied" from "the same text again".
        sentinel = "⁣gusan-selection-%d⁣" % time.monotonic_ns()
        try:
            clip.copy(sentinel)
        except Exception:
            return ""
    try:
        send_copy(kbd)
    except Exception:
        logger.info("Could not send the copy keys", exc_info=True)
        if sentinel is not None and isinstance(before, str):
            clip.copy(before)
        return ""
    deadline = time.monotonic() + wait_s
    text = ""
    while time.monotonic() < deadline:
        time.sleep(0.02)
        if sentinel is None:
            if change_count() != count:
                time.sleep(0.03)                      # let the app finish writing
                text = clip.paste() or ""
                break
        else:
            now = clip.paste()
            if now != sentinel:
                text = now or ""
                break
    try:
        if isinstance(before, str) and (text or sentinel is not None):
            clip.copy(before)                          # the person's clipboard, as it was
    except Exception:
        pass
    return text if isinstance(text, str) else ""


# ── what the app calls ───────────────────────────────────────────────────────────────────────
def enabled():
    """False under the test suites (TRANSCRIBE_NO_SELECTION, or Qt's offscreen platform): a test run must never send
    copy keys to whatever app is in front on the machine."""
    return not os.environ.get("TRANSCRIBE_NO_SELECTION") and not os.environ.get("QT_QPA_PLATFORM", "").startswith("offscreen")


def read_selection(platform=None):
    """The selection in the app in front (Selection; empty when there is none, it is a password, or it can't be
    read). Worker thread."""
    if not enabled():
        return Selection()
    platform = platform or sys.platform
    try:
        if platform == "win32":
            text, has_pattern, read_only, password = _uia_selection()
            if password:
                return Selection()
            if text.strip():
                return _finish(text, "uia", None if read_only is None else not read_only)
            # Nothing from UI Automation: nothing is selected, or the app can't say (Google Docs draws its own text,
            # so Chrome reports an empty selection there). The copy keys ask the app itself - never in a terminal.
            if _foreground_is_terminal() or not _keys_up():
                return Selection()
            return _finish(_copy_selection(), "copy", None if read_only is None else not read_only)
        if platform == "darwin":
            if not _keys_up():
                return Selection()
            return _finish(_copy_selection(), "copy")
    except Exception:
        logger.info("Reading the selection failed", exc_info=True)
    return Selection()


def focus_editable():
    """True / False: the place the cursor is now takes typing (Windows UI Automation's Value pattern); None when it
    can't be told (macOS, a browser, or an app that does not say) - the caller pastes then. Worker thread."""
    if sys.platform != "win32" or not enabled():
        return None
    cls, name = _foreground_window()
    if name in _BROWSERS or cls in ("Chrome_WidgetWin_1", "MozillaWindowClass"):
        return None                     # browsers and the apps built on one (Slack, Teams, Notion...): paste first
    try:
        with _UIA() as uia:
            el = uia.focused()
            if el is None:
                return None
            try:
                read_only = _read_only(el)
            finally:
                _release(el)
            return None if read_only is None else not read_only
    except Exception:
        return None


class SelectionJob:
    """The selection, read on its own thread the moment the shortcut is pressed (the person is already speaking
    meanwhile). ``result(timeout)`` waits for it at most that long."""

    def __init__(self, reader=None):
        self._reader = reader or read_selection
        self._done = threading.Event()
        self._value = Selection()
        threading.Thread(target=self._run, daemon=True, name="gusan-selection").start()

    def _run(self):
        start = time.monotonic()
        try:
            self._value = self._reader() or Selection()
        except Exception:
            logger.info("Selected text: reading it failed", exc_info=True)
            self._value = Selection()
        finally:
            self._done.set()
        # One line per Smart Action (never the text): how it was read, how long, where.
        try:
            program = _foreground_window()[1] if sys.platform == "win32" else ""
        except Exception:
            program = ""
        logger.info("Selected text: %s, %d characters, %d ms%s", self._value.source or "none", len(self._value.text),
                    int((time.monotonic() - start) * 1000), (" (%s)" % program) if program else "")

    def result(self, timeout=1.5):
        self._done.wait(timeout)
        return self._value if self._done.is_set() else Selection()
