"""Generate assets/icon.icns (the macOS app icon) from the Gusan logo (logo.py: the same drawing
the app shows, smooth edges at every size).

Without it PyInstaller gives the Mac bundle its default Python icon. The logo is a full-bleed
circle, so it is drawn on Apple's icon grid (824 px artwork on a 1024 px canvas) to sit at the
same size as the other apps. Run from the repo root before PyInstaller on macOS."""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import logo  # noqa: E402

OUT = os.path.join("assets", "icon.icns")
CANVAS = 1024


def make_icns(out=OUT):
    big = logo.render(CANVAS, logo.MAC_MARGIN)
    # Each size drawn at its own size; Pillow takes the ones it needs from append_images.
    smaller = [logo.render(s, logo.MAC_MARGIN) for s in (16, 32, 64, 128, 256, 512)]
    big.save(out, format="ICNS", append_images=smaller)
    return out


if __name__ == "__main__":
    print(f"Wrote {make_icns()}")
