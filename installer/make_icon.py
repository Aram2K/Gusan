"""Generate assets/icon.ico (the Windows app icon) from the Gusan logo (logo.py: the same drawing
as the app's window and tray icon, smooth edges at every size). Run from the repo root before
invoking Inno Setup."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logo  # noqa: E402

ACCENT = "#3b82f6"
ICO_SIZES = (256, 128, 64, 48, 32, 24, 16)


def make_icon(color=ACCENT, size=256):
    return logo.render(size)


if __name__ == "__main__":
    os.makedirs("assets", exist_ok=True)
    out = "assets/icon.ico"
    # Each size drawn at its own size (a small icon is not a big one shrunk by the .ico writer).
    first, *rest = [logo.render(s) for s in ICO_SIZES]
    first.save(out, format="ICO", sizes=[(s, s) for s in ICO_SIZES], append_images=rest)
    print(f"Wrote {out}")
