"""Who the app is: its name, its version and where its bundled files live.

No Qt and no heavy imports, so services, pages and tests can read these
without loading main.py (which builds the whole controller).

The product is called Gusan. Everything that existing installs depend on
keeps the old technical names and does NOT come from here: the data folder
"Transcribe" and keyring service "TranscribeApp" (storage.py), the installed
TranscribeApp.exe, the installer AppId, and the single-instance port and its
b"transcribe-ok" handshake (main.py). Downloads are also published under
Gusan-* names; the TranscribeApp-* download names stay as identical copies.
"""

import ctypes
import sys
from functools import lru_cache
from pathlib import Path

APP_NAME = "Gusan"
APP_VERSION = "2.1.1"


@lru_cache(maxsize=1)
def is_store_package():
    """Whether Windows assigned package identity to this process."""
    if sys.platform != "win32":
        return False
    try:
        query = ctypes.WinDLL("kernel32", use_last_error=True).GetCurrentPackageFullName
        query.argtypes = [ctypes.POINTER(ctypes.c_uint32), ctypes.c_wchar_p]
        query.restype = ctypes.c_long
        length = ctypes.c_uint32()
        result = query(ctypes.byref(length), None)
        if result == 0:
            return True
        if result != 122 or not length.value:
            return False
        return query(ctypes.byref(length), ctypes.create_unicode_buffer(length.value)) == 0
    except Exception:
        return False

_HERE = Path(__file__).resolve().parent


def resource_path(*parts):
    """Absolute path of a bundled file such as ("assets", "icon_512.png").

    Works from source and from the PyInstaller build (files under
    sys._MEIPASS). Falls back to the source folder when the bundled copy is
    missing, so a dev run always finds the repo's assets."""
    base = Path(getattr(sys, "_MEIPASS", _HERE))
    candidate = base.joinpath(*parts)
    if candidate.exists():
        return str(candidate)
    return str(_HERE.joinpath(*parts))
