"""Windows global shortcuts that never reach the app in front.

The shortcuts (Alt+R dictation, Alt+S Smart Actions, Alt+E Live Assistant) were a keyboard hook that let the keys
through (keyboard.add_hotkey(suppress=False)): in a text box - Gusan's own, a browser's - Alt+S also typed an "s"
(the founder, 2026-11-08). Here they are registered with Windows itself (RegisterHotKey): the system takes the
combination, so the app in front never sees it (no letter typed, no Alt menu).

RegisterHotKey binds a shortcut to the thread that registers it, and that thread must run a message loop to receive
WM_HOTKEY: one daemon thread owns every registration and its loop; register() / unregister() hand it the work and
wait for the answer. A combination another program already registered is refused (register() returns None) - the
caller then keeps the old hook (main.AppController._add_win_hotkey).

``parse("ctrl+shift+space")`` -> (modifiers, virtual key) or None for what RegisterHotKey cannot take (mouse
buttons, a key name it does not know). Qt-free; Windows only (``available()``).
"""
import logging
import sys
import threading

logger = logging.getLogger(__name__)

MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN, MOD_NOREPEAT = 0x0001, 0x0002, 0x0004, 0x0008, 0x4000
WM_HOTKEY, WM_APP_WORK = 0x0312, 0x8000 + 0x4C

_MODS = {"ctrl": MOD_CONTROL, "control": MOD_CONTROL, "alt": MOD_ALT, "shift": MOD_SHIFT,
         "win": MOD_WIN, "windows": MOD_WIN, "cmd": MOD_WIN, "super": MOD_WIN}
_KEYS = {"space": 0x20, "enter": 0x0D, "return": 0x0D, "tab": 0x09, "esc": 0x1B, "escape": 0x1B,
         "backspace": 0x08, "insert": 0x2D, "delete": 0x2E, "del": 0x2E, "home": 0x24, "end": 0x23,
         "page up": 0x21, "pageup": 0x21, "page down": 0x22, "pagedown": 0x22, "left": 0x25, "up": 0x26,
         "right": 0x27, "down": 0x28, "print screen": 0x2C, "pause": 0x13, "caps lock": 0x14,
         ";": 0xBA, "=": 0xBB, ",": 0xBC, "-": 0xBD, ".": 0xBE, "/": 0xBF, "`": 0xC0, "[": 0xDB, "\\": 0xDC,
         "]": 0xDD, "'": 0xDE}


def parse(hotkey):
    """(modifiers, virtual key) for RegisterHotKey, or None (a mouse button, an unknown key, no key at all)."""
    text = str(hotkey or "").strip().lower()
    if not text or text.startswith("mouse:"):
        return None
    mods, key = 0, None
    for part in [p.strip() for p in text.split("+")]:
        if not part:
            continue
        if part in _MODS:
            mods |= _MODS[part]
            continue
        if key is not None:
            return None                          # two plain keys: not a shortcut Windows can take
        if len(part) == 1 and ("a" <= part <= "z" or "0" <= part <= "9"):
            key = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit() and 1 <= int(part[1:]) <= 24:
            key = 0x6F + int(part[1:])
        elif part in _KEYS:
            key = _KEYS[part]
        else:
            return None
    return (mods, key) if key is not None else None


def available():
    """Windows, and not switched off (TRANSCRIBE_NO_WIN_HOTKEYS=1: the test runs never take real shortcuts)."""
    import os
    return sys.platform == "win32" and os.environ.get("TRANSCRIBE_NO_WIN_HOTKEYS") != "1"


class _Registrar:
    """The thread that owns the registrations and receives WM_HOTKEY."""

    def __init__(self):
        import ctypes
        from ctypes import wintypes
        self._ctypes, self._wintypes = ctypes, wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._user32.RegisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int, wintypes.UINT, wintypes.UINT]
        self._user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
        self._user32.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self._callbacks = {}                     # id -> callback
        self._work = []                          # (function, result dict, event)
        self._lock = threading.Lock()
        self._next_id = 0xB000
        self._ready = threading.Event()
        self._thread_id = 0
        threading.Thread(target=self._loop, daemon=True, name="gusan-hotkeys").start()
        self._ready.wait(3)

    def _loop(self):
        ctypes, wintypes = self._ctypes, self._wintypes
        msg = wintypes.MSG()
        self._user32.PeekMessageW(ctypes.byref(msg), None, 0, 0, 0)      # the thread's message queue exists now
        self._thread_id = self._kernel32.GetCurrentThreadId()
        self._ready.set()
        while self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            if msg.message == WM_HOTKEY:
                cb = self._callbacks.get(int(msg.wParam))
                if cb is not None:
                    try:
                        cb()
                    except Exception:
                        logger.exception("A shortcut's action failed")
            elif msg.message == WM_APP_WORK:
                with self._lock:
                    work, self._work = self._work, []
                for fn, result, done in work:
                    try:
                        result["value"] = fn()
                    except Exception as e:
                        result["error"] = e
                    done.set()

    def _run(self, fn, timeout=3.0):
        if not self._thread_id:
            raise OSError("the shortcut thread did not start")
        result, done = {}, threading.Event()
        with self._lock:
            self._work.append((fn, result, done))
        self._user32.PostThreadMessageW(self._thread_id, WM_APP_WORK, 0, 0)
        if not done.wait(timeout):
            raise OSError("the shortcut thread did not answer")
        if "error" in result:
            raise result["error"]
        return result.get("value")

    def register(self, mods, vk, callback):
        def do():
            self._next_id += 1
            hid = self._next_id
            if not self._user32.RegisterHotKey(None, hid, mods | MOD_NOREPEAT, vk):
                return None, self._ctypes.get_last_error()
            self._callbacks[hid] = callback
            return hid, 0
        return self._run(do)

    def unregister(self, hid):
        def do():
            self._callbacks.pop(hid, None)
            return bool(self._user32.UnregisterHotKey(None, hid))
        return self._run(do)


_registrar = None
_registrar_lock = threading.Lock()


def _get():
    global _registrar
    with _registrar_lock:
        if _registrar is None:
            _registrar = _Registrar()
        return _registrar


def register(hotkey, callback):
    """Register ``hotkey`` ("alt+s") with Windows: ``callback()`` runs on the shortcut thread at each press (it
    should only hand the work over, e.g. emit a Qt signal). The id, or None when it cannot (not Windows, a key
    Windows cannot take, a combination another program has)."""
    parsed = parse(hotkey) if available() else None
    if parsed is None:
        return None
    try:
        hid, err = _get().register(parsed[0], parsed[1], callback)
    except Exception:
        logger.info("Shortcut %s: Windows registration failed", hotkey, exc_info=True)
        return None
    if hid is None:
        logger.info("Shortcut %s is taken by another program (error %s): the keyboard hook is used", hotkey, err)
    return hid


def unregister(hid):
    if hid is None or _registrar is None:
        return False
    try:
        return bool(_registrar.unregister(hid))
    except Exception:
        logger.debug("Unregistering a shortcut failed", exc_info=True)
        return False
