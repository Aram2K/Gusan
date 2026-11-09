"""macOS privacy permissions: read whether each one is granted, ask macOS for
it, and open the right System Settings pane. Qt-free.

Gusan needs three on a Mac, and without any of them it looks silently broken:

  * Microphone        - hear the voice (dictation, meetings).
  * Accessibility     - paste the finished text, and run the hotkey listener
                        (pynput's event tap logs "This process is not trusted!"
                        and hears nothing without it).
  * Input Monitoring  - hear the global hotkey from any app.

status() reads them live, so the guide can show what is on and what is not and
notice the moment the user switches one on. request() is the one-click path:
for Accessibility and Input Monitoring it asks macOS to show its own prompt,
which also ADDS Gusan to the list in System Settings, so the user only has to
flip a switch instead of hunting for the app with "+".

Who macOS asks about: the installed app is Gusan itself. Run from source, it is
the app that started Python (Terminal, an IDE ...), so that is the one to allow.

Everything is ctypes against the system frameworks, with exact prototypes
(arm64 must never call objc_msgSend through a variadic signature). Off macOS
every call is a no-op: status() says None ("can't tell")."""
import ctypes
import logging
import os
import subprocess
import sys

logger = logging.getLogger(__name__)

IS_MAC = sys.platform == "darwin"

MICROPHONE = "microphone"
ACCESSIBILITY = "accessibility"
INPUT_MONITORING = "input_monitoring"
ALL = (MICROPHONE, ACCESSIBILITY, INPUT_MONITORING)
# The Interpreter's Hear only (the call's sound, a Core Audio process tap): not in the start guide.
SYSTEM_AUDIO = "system_audio"

_PANE = "x-apple.systempreferences:com.apple.preference.security?"
SETTINGS_URLS = {
    MICROPHONE: _PANE + "Privacy_Microphone",
    ACCESSIBILITY: _PANE + "Privacy_Accessibility",
    INPUT_MONITORING: _PANE + "Privacy_ListenEvent",
    SYSTEM_AUDIO: _PANE + "Privacy_AudioCapture",
}

# IOHIDCheckAccess / IOHIDRequestAccess (IOKit/hid/IOHIDLib.h)
_HID_LISTEN_EVENT = 1
_HID_GRANTED, _HID_DENIED, _HID_UNKNOWN = 0, 1, 2
# AVAuthorizationStatus
_AV_NOT_DETERMINED, _AV_RESTRICTED, _AV_DENIED, _AV_AUTHORIZED = 0, 1, 2, 3

_libs = {}


def _lib(name, path):
    if name not in _libs:
        _libs[name] = ctypes.cdll.LoadLibrary(path)
    return _libs[name]


def _ax():
    return _lib("ax", "/System/Library/Frameworks/ApplicationServices.framework/ApplicationServices")


def _iokit():
    return _lib("iokit", "/System/Library/Frameworks/IOKit.framework/IOKit")


def _cf():
    return _lib("cf", "/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation")


# ── reading ──

def accessibility_granted():
    """True / False, or None when it can't be read (not a Mac)."""
    if not IS_MAC:
        return None
    try:
        fn = _ax().AXIsProcessTrusted
        fn.restype = ctypes.c_bool
        fn.argtypes = []
        return bool(fn())
    except Exception as e:
        logger.debug("AXIsProcessTrusted failed: %s", e)
        return None


def input_monitoring_granted():
    """True / False, or None when macOS hasn't decided yet or can't say. An
    app that never asked is "unknown" (None) until it listens or requests."""
    if not IS_MAC:
        return None
    try:
        fn = _iokit().IOHIDCheckAccess
        fn.restype = ctypes.c_uint32
        fn.argtypes = [ctypes.c_uint32]
        res = fn(_HID_LISTEN_EVENT)
    except Exception as e:
        logger.debug("IOHIDCheckAccess failed: %s", e)
        return None
    if res == _HID_GRANTED:
        return True
    if res == _HID_DENIED:
        return False
    return None


def _objc():
    lib = _lib("objc", "/usr/lib/libobjc.A.dylib")
    lib.objc_getClass.restype = ctypes.c_void_p
    lib.objc_getClass.argtypes = [ctypes.c_char_p]
    lib.sel_registerName.restype = ctypes.c_void_p
    lib.sel_registerName.argtypes = [ctypes.c_char_p]
    return lib


def _mic_status():
    """AVCaptureDevice authorizationStatusForMediaType:AVMediaTypeAudio, or None."""
    if not IS_MAC:
        return None
    try:
        av = _lib("av", "/System/Library/Frameworks/AVFoundation.framework/AVFoundation")
        objc = _objc()
        media_audio = ctypes.c_void_p.in_dll(av, "AVMediaTypeAudio").value
        cls = objc.objc_getClass(b"AVCaptureDevice")
        sel = objc.sel_registerName(b"authorizationStatusForMediaType:")
        send = ctypes.CFUNCTYPE(ctypes.c_long, ctypes.c_void_p, ctypes.c_void_p,
                                ctypes.c_void_p)(("objc_msgSend", objc))
        return int(send(cls, sel, media_audio))
    except Exception as e:
        logger.debug("AVCaptureDevice status failed: %s", e)
        return None


def microphone_granted():
    """True / False, or None when macOS hasn't asked yet (it will, the first
    time Gusan opens the microphone) or can't say."""
    st = _mic_status()
    if st == _AV_AUTHORIZED:
        return True
    if st in (_AV_DENIED, _AV_RESTRICTED):
        return False
    return None


def system_audio_granted():
    """"System Audio Recording" (the Interpreter hears the call): True / False, or None when
    macOS hasn't asked yet or can't say. Read with TCC's own preflight (no prompt)."""
    if not IS_MAC:
        return None
    try:
        tcc = _lib("tcc", "/System/Library/PrivateFrameworks/TCC.framework/Versions/A/TCC")
        cf = _cf()
        cf.CFStringCreateWithCString.restype = ctypes.c_void_p
        cf.CFStringCreateWithCString.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_uint32]
        cf.CFRelease.argtypes = [ctypes.c_void_p]
        service = cf.CFStringCreateWithCString(None, b"kTCCServiceAudioCapture", 0x08000100)
        fn = tcc.TCCAccessPreflight
        fn.restype = ctypes.c_int
        fn.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        try:
            res = fn(service, None)
        finally:
            cf.CFRelease(service)
    except Exception as e:
        logger.debug("TCCAccessPreflight failed: %s", e)
        return None
    return True if res == 0 else False if res == 1 else None


def status():
    """{name: True | False | None} for the three permissions. None = not
    decided yet / can't tell; the guide shows it as "not on yet"."""
    return {
        MICROPHONE: microphone_granted(),
        ACCESSIBILITY: accessibility_granted(),
        INPUT_MONITORING: input_monitoring_granted(),
    }


def missing(st=None):
    """The permissions that are not on, in ALL's order. Empty off macOS."""
    if not IS_MAC:
        return []
    st = status() if st is None else st
    return [k for k in ALL if st.get(k) is not True]


def hotkeys_blocked(st=None):
    """True when the global hotkey can't work: Accessibility or Input
    Monitoring is not on. False off macOS."""
    if not IS_MAC:
        return False
    st = status() if st is None else st
    return st.get(ACCESSIBILITY) is not True or st.get(INPUT_MONITORING) is False


# ── asking ──

def _request_accessibility():
    """AXIsProcessTrustedWithOptions({kAXTrustedCheckOptionPrompt: true}):
    macOS adds the app to the Accessibility list and shows its own prompt with
    an "Open System Settings" button. Returns the current state."""
    ax, cf = _ax(), _cf()
    key = ctypes.c_void_p.in_dll(ax, "kAXTrustedCheckOptionPrompt").value
    true = ctypes.c_void_p.in_dll(cf, "kCFBooleanTrue").value
    key_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryKeyCallBacks"))
    val_cb = ctypes.addressof(ctypes.c_char.in_dll(cf, "kCFTypeDictionaryValueCallBacks"))
    cf.CFDictionaryCreate.restype = ctypes.c_void_p
    cf.CFDictionaryCreate.argtypes = [ctypes.c_void_p, ctypes.POINTER(ctypes.c_void_p),
                                      ctypes.POINTER(ctypes.c_void_p), ctypes.c_long,
                                      ctypes.c_void_p, ctypes.c_void_p]
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    keys = (ctypes.c_void_p * 1)(key)
    vals = (ctypes.c_void_p * 1)(true)
    opts = cf.CFDictionaryCreate(None, keys, vals, 1, key_cb, val_cb)
    try:
        fn = ax.AXIsProcessTrustedWithOptions
        fn.restype = ctypes.c_bool
        fn.argtypes = [ctypes.c_void_p]
        return bool(fn(opts))
    finally:
        if opts:
            cf.CFRelease(opts)


def _request_input_monitoring():
    """IOHIDRequestAccess: adds the app to the Input Monitoring list and, the
    first time, shows macOS's prompt. Returns True when granted."""
    fn = _iokit().IOHIDRequestAccess
    fn.restype = ctypes.c_bool
    fn.argtypes = [ctypes.c_uint32]
    return bool(fn(_HID_LISTEN_EVENT))


def open_settings(name):
    """Open System Settings on the pane for ``name``. True when asked."""
    url = SETTINGS_URLS.get(name)
    if not IS_MAC or not url:
        return False
    try:
        subprocess.Popen(["open", url])
        return True
    except Exception as e:
        logger.warning("Could not open System Settings (%s): %s", name, e)
        return False


def request(name):
    """The guide's one button for ``name``: ask macOS for it, then open the
    pane where the switch is. For Accessibility and Input Monitoring the ask
    puts Gusan in the list first, so the user only flips it on. The microphone
    is asked for the first time Gusan records (macOS's own prompt); once that
    was answered only System Settings can change it. Returns True when the
    permission is already on (nothing to open)."""
    if not IS_MAC:
        return False
    try:
        if name == ACCESSIBILITY and _request_accessibility():
            return True
        if name == INPUT_MONITORING and _request_input_monitoring():
            return True
        if name == MICROPHONE and microphone_granted():
            return True
    except Exception as e:
        logger.warning("Asking macOS for %s failed: %s", name, e)
    open_settings(name)
    return False


# ── restarting ──
# macOS applies a new Input Monitoring grant to an app only after it is
# reopened (its own "Quit & Reopen" button). The installed app can do that by
# itself; run from source, the user reopens it.

def app_bundle(executable=None):
    """The installed app's .app folder (…/TranscribeApp.app), or None when
    Gusan runs from source (not frozen) or not on a Mac."""
    if not IS_MAC or not getattr(sys, "frozen", False):
        return None
    exe = os.path.abspath(executable or sys.executable)
    bundle = os.path.dirname(os.path.dirname(os.path.dirname(exe)))   # .app/Contents/MacOS/exe
    return bundle if bundle.endswith(".app") else None


def relaunch_after_exit(pid=None, bundle=None, popen=subprocess.Popen):
    """Start a tiny shell that waits for this process to end, then opens the
    app again. Call it, then quit. Returns the started process (terminate it
    when the quit doesn't happen), or False when it couldn't start."""
    bundle = bundle or app_bundle()
    if not bundle:
        return False
    pid = int(pid or os.getpid())
    script = 'while kill -0 "$1" 2>/dev/null; do sleep 0.3; done; exec open "$2"'
    try:
        proc = popen(["/bin/sh", "-c", script, "gusan-relaunch", str(pid), bundle],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True, close_fds=True)
        return proc if proc is not None else True
    except Exception as e:
        logger.warning("Could not schedule the relaunch: %s", e)
        return False
