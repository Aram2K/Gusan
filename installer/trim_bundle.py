"""Remove parts of the Windows bundle the app never uses, after PyInstaller and before the zip and the installer
(2026-11-16 size audit: about 28 MB of the 116 MB installer, about 75 MB installed).

    python installer/trim_bundle.py dist/TranscribeApp            # remove them (the build)
    python installer/trim_bundle.py dist/TranscribeApp --check    # only say what it would remove

What goes (Windows only - the Mac's Qt plays recordings through FFmpeg, and is left as it is):
  * Qt Quick / QML and the virtual keyboard: only Qt's virtual-keyboard plugin loads them; Gusan is Qt Widgets.
  * Qt PDF: only Qt's "read a PDF as a picture" plugin loads it; Gusan reads PDFs with pypdf.
  * Qt's own FFmpeg and its media plugin: Gusan plays recordings through Windows' Media Foundation
    (QT_MEDIA_BACKEND=windows, ui/components/audio_player.py); the build checks that plugin is there.
  * Qt's translations (Gusan installs no QTranslator) and the Direct2D platform plugin (Qt uses "windows").
  * pytest and pygments: test tools the build machine has installed, picked up by --collect-all.

Safe by construction: before anything is removed, every program file that stays (.exe .dll .pyd) is read, and the
build FAILS if one of them still links to a file on the list - a removal can only cost a feature nobody uses,
never a program that does not start. Never touches anything outside the bundle folder given.
"""
import argparse
import fnmatch
import os
import shutil
import sys

# Paths inside <bundle>/_internal, with * wildcards; a folder goes whole.
REMOVE = (
    # Qt Quick / QML / the virtual keyboard
    "PySide6/Qt6Quick*.dll", "PySide6/Qt6Qml*.dll", "PySide6/Qt6VirtualKeyboard*.dll",
    "PySide6/Qt6OpenGL.dll", "PySide6/Qt6OpenGLWidgets.dll",
    "PySide6/plugins/platforminputcontexts/qtvirtualkeyboardplugin.dll",
    "PySide6/qml",
    # Qt PDF
    "PySide6/Qt6Pdf*.dll", "PySide6/plugins/imageformats/qpdf.dll",
    # Qt's FFmpeg (Windows plays through Media Foundation)
    "PySide6/avcodec-*.dll", "PySide6/avformat-*.dll", "PySide6/avutil-*.dll", "PySide6/swscale-*.dll",
    "PySide6/swresample-*.dll", "PySide6/plugins/multimedia/ffmpegmediaplugin.dll",
    # translations, another platform plugin
    "PySide6/translations", "PySide6/plugins/platforms/qdirect2d.dll",
    # test tools
    "pytest", "_pytest", "pytest-*.dist-info", "pygments", "pygments-*.dist-info",
)
# Never removed, whatever the list says: what the app runs on.
KEEP = ("PySide6/Qt6Core.dll", "PySide6/Qt6Gui.dll", "PySide6/Qt6Widgets.dll", "PySide6/Qt6Svg.dll",
        "PySide6/Qt6Multimedia.dll", "PySide6/Qt6Network.dll", "PySide6/plugins/platforms/qwindows.dll",
        "PySide6/plugins/multimedia/windowsmediaplugin.dll")
PROGRAM_EXT = (".exe", ".dll", ".pyd")


def _rel(path, base):
    return os.path.relpath(path, base).replace(os.sep, "/")


def plan(bundle):
    """[(path, bytes)] of what REMOVE matches in <bundle>/_internal (a folder: its total)."""
    base = os.path.join(bundle, "_internal")
    out = []
    for root, dirs, files in os.walk(base):
        for name in list(dirs) + files:
            path = os.path.join(root, name)
            rel = _rel(path, base)
            if not any(fnmatch.fnmatch(rel, pat) for pat in REMOVE) or rel in KEEP:
                continue
            if os.path.isdir(path):
                size = sum(os.path.getsize(os.path.join(r, f)) for r, _d, fs in os.walk(path) for f in fs)
                dirs.remove(name)                 # (gone whole: nothing inside it is looked at again)
            else:
                size = os.path.getsize(path)
            out.append((path, size))
    return out


def _imports(path):
    """The DLL names a program file links to (lower case), or None when it can't be read."""
    try:
        import pefile
    except ImportError:
        return None
    try:
        pe = pefile.PE(path, fast_load=True)
        pe.parse_data_directories(directories=[pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_IMPORT"],
                                               pefile.DIRECTORY_ENTRY["IMAGE_DIRECTORY_ENTRY_DELAY_IMPORT"]])
        names = set()
        for attr in ("DIRECTORY_ENTRY_IMPORT", "DIRECTORY_ENTRY_DELAY_IMPORT"):
            for entry in getattr(pe, attr, []) or []:
                names.add(entry.dll.decode("ascii", "ignore").lower())
        pe.close()
        return names
    except Exception:
        return None


def still_needed(bundle, removing):
    """[(file that stays, removed DLL it links to)]: what the removal would break."""
    gone = set()
    gone_dirs = []
    for path, _size in removing:
        if os.path.isdir(path):
            gone_dirs.append(os.path.normcase(path) + os.sep)
            for r, _d, fs in os.walk(path):
                gone.update(f.lower() for f in fs if f.lower().endswith(".dll"))
        elif path.lower().endswith(".dll"):
            gone.add(os.path.basename(path).lower())
    removing_paths = {os.path.normcase(p) for p, _s in removing}
    broken = []
    for root, _dirs, files in os.walk(bundle):
        for f in files:
            path = os.path.join(root, f)
            norm = os.path.normcase(path)
            if norm in removing_paths or any(norm.startswith(d) for d in gone_dirs):
                continue
            if not f.lower().endswith(PROGRAM_EXT):
                continue
            names = _imports(path)
            if names is None:
                continue
            for dll in sorted(names & gone):
                broken.append((_rel(path, bundle), dll))
    return broken


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("bundle", help="the PyInstaller folder (dist/TranscribeApp)")
    ap.add_argument("--check", action="store_true", help="say what would go, remove nothing")
    a = ap.parse_args(argv)
    if sys.platform != "win32" and not a.check:
        print("trim_bundle: not Windows - nothing removed")
        return 0
    if not os.path.isdir(os.path.join(a.bundle, "_internal")):
        print("trim_bundle: no _internal folder in %s" % a.bundle)
        return 1
    try:
        import pefile  # noqa: F401
    except ImportError:
        print("trim_bundle: pefile is missing (it comes with PyInstaller): nothing removed")
        return 1
    removing = plan(a.bundle)
    total = sum(s for _p, s in removing)
    for path, size in sorted(removing, key=lambda x: -x[1]):
        print("  %8.1f MB  %s" % (size / 1e6, _rel(path, a.bundle)))
    print("trim_bundle: %d items, %.1f MB" % (len(removing), total / 1e6))
    broken = still_needed(a.bundle, removing)
    if broken:
        for f, dll in broken:
            print("::error title=trim_bundle::%s links to %s - not removing anything" % (f, dll))
        return 2
    if a.check:
        print("trim_bundle: --check, nothing removed (nothing that stays links to these)")
        return 0
    for path, _size in removing:
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.exists(path):
            os.remove(path)
    print("trim_bundle: removed %.1f MB" % (total / 1e6))
    return 0


if __name__ == "__main__":
    sys.exit(main())
