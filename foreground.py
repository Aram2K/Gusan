"""Best-effort external application focus tokens. No Qt dependency."""
import ctypes
from ctypes import wintypes
from dataclasses import dataclass
import os
import sys


@dataclass(frozen=True)
class Target:
    platform: str
    pid: int
    name: str
    hwnd: int = 0


def _windows():
    u = ctypes.WinDLL("user32", use_last_error=True)
    signatures = {
        "GetForegroundWindow": (wintypes.HWND, []),
        "GetWindowThreadProcessId": (wintypes.DWORD, [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]),
        "GetWindowTextW": (ctypes.c_int, [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]),
        "IsWindow": (wintypes.BOOL, [wintypes.HWND]),
        "IsIconic": (wintypes.BOOL, [wintypes.HWND]),
        "ShowWindow": (wintypes.BOOL, [wintypes.HWND, ctypes.c_int]),
        "SetForegroundWindow": (wintypes.BOOL, [wintypes.HWND]),
        "AllowSetForegroundWindow": (wintypes.BOOL, [wintypes.DWORD]),
        "AttachThreadInput": (wintypes.BOOL, [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]),
        "MonitorFromWindow": (wintypes.HANDLE, [wintypes.HWND, wintypes.DWORD]),
        "GetMonitorInfoW": (wintypes.BOOL, [wintypes.HANDLE, ctypes.c_void_p]),
    }
    for name, (restype, args) in signatures.items():
        fn = getattr(u, name)
        fn.restype, fn.argtypes = restype, args
    return u


def _mac():
    import mac_permissions
    mac_permissions._lib("appkit", "/System/Library/Frameworks/AppKit.framework/AppKit")
    lib = mac_permissions._objc()

    def send(obj, selector, restype=ctypes.c_void_p, argtypes=(), args=()):
        fn = ctypes.CFUNCTYPE(restype, ctypes.c_void_p, ctypes.c_void_p,
                              *argtypes)(("objc_msgSend", lib))
        return fn(obj, lib.sel_registerName(selector), *args)

    return lib, send


def remember():
    """Return an external app token (with a display name), or None."""
    try:
        if sys.platform == "win32":
            u = _windows()
            hwnd = u.GetForegroundWindow()
            pid = wintypes.DWORD()
            u.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if not hwnd or not pid.value or pid.value == os.getpid():
                return None
            title = ctypes.create_unicode_buffer(512)
            u.GetWindowTextW(hwnd, title, len(title))
            name = title.value or "previous app"
            try:
                import psutil
                name = os.path.splitext(psutil.Process(pid.value).name())[0] or name
            except Exception:
                pass
            return Target("win32", pid.value, name, int(hwnd))
        if sys.platform == "darwin":
            lib, send = _mac()
            workspace = send(lib.objc_getClass(b"NSWorkspace"), b"sharedWorkspace")
            app = send(workspace, b"frontmostApplication")
            pid = send(app, b"processIdentifier", ctypes.c_int)
            if not app or not pid or pid == os.getpid():
                return None
            name = send(send(app, b"localizedName"), b"UTF8String", ctypes.c_char_p)
            return Target("darwin", pid, name.decode("utf-8", "replace") if name else "previous app")
    except Exception:
        pass
    return None


def _valid_window(u, token):
    pid = wintypes.DWORD()
    u.GetWindowThreadProcessId(token.hwnd, ctypes.byref(pid))
    return bool(u.IsWindow(token.hwnd) and pid.value == token.pid and pid.value != os.getpid())


def restore(token):
    """Reactivate a remembered target; False when unavailable or denied by OS."""
    try:
        if not isinstance(token, Target) or token.pid == os.getpid() or token.platform != sys.platform:
            return False
        if sys.platform == "win32":
            u = _windows()
            if not _valid_window(u, token):
                return False
            if u.IsIconic(token.hwnd):
                u.ShowWindow(token.hwnd, 9)  # SW_RESTORE
            u.AllowSetForegroundWindow(token.pid)
            if u.SetForegroundWindow(token.hwnd):
                return True
            kernel = ctypes.WinDLL("kernel32", use_last_error=True)
            kernel.GetCurrentThreadId.restype = wintypes.DWORD
            kernel.GetCurrentThreadId.argtypes = []
            current = kernel.GetCurrentThreadId()
            foreground = u.GetWindowThreadProcessId(u.GetForegroundWindow(), None)
            attached = foreground != current and u.AttachThreadInput(current, foreground, True)
            try:
                return bool(u.SetForegroundWindow(token.hwnd))
            finally:
                if attached:
                    u.AttachThreadInput(current, foreground, False)
        if sys.platform == "darwin":
            lib, send = _mac()
            app = send(lib.objc_getClass(b"NSRunningApplication"),
                       b"runningApplicationWithProcessIdentifier:",
                       argtypes=(ctypes.c_int,), args=(token.pid,))
            return bool(app and send(app, b"activateWithOptions:", ctypes.c_bool,
                                     (ctypes.c_ulong,), (2,)))
    except Exception:
        pass
    return False


def monitor_rect(token):
    """Physical-pixel (x, y, width, height), or None for the UI's fallback."""
    try:
        if not isinstance(token, Target) or token.platform != sys.platform or sys.platform != "win32":
            return None
        u = _windows()
        if not _valid_window(u, token):
            return None

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        info = MonitorInfo()
        info.cbSize = ctypes.sizeof(info)
        monitor = u.MonitorFromWindow(token.hwnd, 2)
        if monitor and u.GetMonitorInfoW(monitor, ctypes.byref(info)):
            r = info.rcMonitor
            return r.left, r.top, r.right - r.left, r.bottom - r.top
    except Exception:
        pass
    return None
