"""Generate assets/icon.icns (the macOS app icon) from assets/icon_1024.png,
the same Gusan logo the app shows in its windows (ui/components/icons.py).

Without it PyInstaller gives the Mac bundle its default Python icon, which is
what Launchpad, the Dock and Finder showed. The logo is a full-bleed circle,
so it is drawn on Apple's icon grid (824 px artwork on a 1024 px canvas) to
sit at the same size as the other apps. Run from the repo root before
PyInstaller on macOS."""
import os

from PIL import Image

SRC = os.path.join("assets", "icon_1024.png")
OUT = os.path.join("assets", "icon.icns")
CANVAS = 1024
ART = 824                                   # Apple's macOS icon grid


def make_icns(src=SRC, out=OUT):
    logo = Image.open(src).convert("RGBA").resize((ART, ART), Image.LANCZOS)
    img = Image.new("RGBA", (CANVAS, CANVAS), (0, 0, 0, 0))
    off = (CANVAS - ART) // 2
    img.paste(logo, (off, off), logo)
    # Pillow writes every size an .icns needs (16 ... 1024, with @2x) from this one.
    img.save(out, format="ICNS")
    return out


if __name__ == "__main__":
    print(f"Wrote {make_icns()}")
