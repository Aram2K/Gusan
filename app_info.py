"""Who the app is: its name, its version and where its bundled files live.

No Qt and no heavy imports, so services, pages and tests can read these
without loading main.py (which builds the whole controller).

The product is called Gusan. Everything that existing installs depend on
keeps the old technical names and does NOT come from here: the data folder
"Transcribe" and keyring service "TranscribeApp" (storage.py), the
TranscribeApp.exe / TranscribeApp-Windows-Setup.exe file names, the
single-instance port and its b"transcribe-ok" handshake (main.py).
"""

import sys
from pathlib import Path

APP_NAME = "Gusan"
APP_VERSION = "2.0.1"

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
