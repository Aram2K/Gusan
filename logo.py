"""The Gusan logo, drawn sharp at any size. Qt-free (Pillow only).

One drawing for every place the logo appears: the window / Dock / taskbar icon and the tray icon
(main.make_qicon), the Windows .ico (installer/make_icon.py), the macOS .icns
(installer/make_icns.py) and the logo images in assets/ (the sidebar's icons.logo_pixmap).

The design is the one the app always had, on a 64-unit grid: the Armenian flag's three stripes in
a circle, with a white microphone. It used to be drawn straight at the target size with no
smoothing (hard, stair-stepped edges - and the app icon was only 64 px, stretched by the Dock).
Here every shape is drawn SUPERSAMPLE times larger and scaled down with Lanczos filtering, so its
edges are smooth at 16 px and at 1024 px alike.

    logo.render(1024)                    # the logo filling the square (the assets, Windows)
    logo.render(1024, margin=MAC_MARGIN) # on Apple's icon grid (the Dock, the .icns)
"""
from PIL import Image, ImageDraw

RED = (217, 30, 24, 255)        # #d91e18
BLUE = (0, 51, 160, 255)        # #0033a0
ORANGE = (242, 168, 21, 255)    # #f2a815
WHITE = (255, 255, 255, 255)

SUPERSAMPLE = 4
# Apple's macOS icon grid: the artwork is 824 px of a 1024 px canvas, so the icon sits at the same
# size as the other apps in the Dock and Launchpad.
MAC_MARGIN = (1024 - 824) / 2 / 1024
# The sizes an app icon carries (each drawn at its own size, never stretched).
ICON_SIZES = (16, 24, 32, 48, 64, 128, 256, 512, 1024)


def render(size, margin=0.0):
    """The logo as a ``size`` x ``size`` RGBA image; ``margin``: the share of the side left empty
    around it (0 = edge to edge)."""
    size = int(size)
    big = size * SUPERSAMPLE
    off = big * float(margin)
    k = (big - 2 * off) / 64.0

    def box(x0, y0, x1, y1):
        return [off + x0 * k, off + y0 * k, off + x1 * k, off + y1 * k]

    # The circle: everything is drawn inside it (the microphone's foot too).
    circle = Image.new("L", (big, big), 0)
    ImageDraw.Draw(circle).ellipse(box(2, 2, 62, 62), fill=255)

    art = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    d = ImageDraw.Draw(art)
    d.rectangle(box(0, 0, 64, 22), fill=RED)
    d.rectangle(box(0, 22, 64, 42), fill=BLUE)
    d.rectangle(box(0, 42, 64, 64), fill=ORANGE)
    # The microphone: its body, the bowl it sits in, the stem and the foot.
    d.rectangle(box(24, 12, 40, 40), fill=WHITE)
    d.ellipse(box(18, 32, 46, 52), fill=WHITE)
    d.rectangle(box(30, 50, 34, 60), fill=WHITE)
    d.rectangle(box(22, 58, 42, 62), fill=WHITE)

    out = Image.new("RGBA", (big, big), (0, 0, 0, 0))
    out.paste(art, (0, 0), circle)
    return out.resize((size, size), Image.LANCZOS)


def save_assets(folder="assets"):
    """Draw the logo images the app ships (icon_512 / 1024 / 2048, icon_preview at 256)."""
    import os
    for name, size in (("icon_512.png", 512), ("icon_1024.png", 1024), ("icon_2048.png", 2048),
                       ("icon_preview.png", 256)):
        render(size).save(os.path.join(folder, name), optimize=True)


if __name__ == "__main__":
    save_assets()
    print("Wrote the logo images in assets/")
