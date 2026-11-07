"""Generate a Store layout on any platform; pack it with the Windows SDK."""
import argparse
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from PIL import Image

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import logo


def package_version(version):
    if not re.fullmatch(r"\d+\.\d+\.\d+", version):
        raise ValueError("Version must be X.Y.Z")
    parts = [int(p) for p in version.split(".")]
    if not 1 <= parts[0] <= 65535 or any(p > 65535 for p in parts[1:]):
        raise ValueError("Version components must fit the MSIX version range")
    return ".".join(map(str, parts)) + ".0"


def generate_layout(source, layout, version, identity_name="AIBUBEN.Gusan",
                    publisher="CN=00000000-0000-0000-0000-000000000000",
                    publisher_display_name="AIBUBEN"):
    source, layout = Path(source).resolve(), Path(layout).resolve()
    version = package_version(version)
    if not all((identity_name, publisher, publisher_display_name)):
        raise ValueError("Package identity values must not be empty")
    if source == layout or source in layout.parents or layout in source.parents:
        raise ValueError("Source and layout must be separate directories")
    if not (source / "TranscribeApp.exe").is_file():
        raise ValueError("Source must contain TranscribeApp.exe")
    if layout.exists():
        raise FileExistsError("Use a fresh layout directory: " + str(layout))
    shutil.copytree(source, layout)
    assets = layout / "Assets"
    assets.mkdir(exist_ok=True)
    for name, size in (("Square44x44Logo", (44, 44)), ("Square150x150Logo", (150, 150)),
                       ("Wide310x150Logo", (310, 150)), ("StoreLogo", (50, 50)),
                       ("Square44x44Logo.targetsize-44_altform-unplated", (44, 44))):
        canvas = Image.new("RGBA", size, (0, 0, 0, 0))
        mark = logo.render(min(size)).convert("RGBA")
        canvas.alpha_composite(mark, ((size[0] - mark.width) // 2, (size[1] - mark.height) // 2))
        canvas.save(assets / (name + ".png"))
    manifest = (Path(__file__).parent / "msix" / "AppxManifest.xml").read_text(encoding="utf-8")
    for key, value in {"IDENTITY_NAME": identity_name, "PUBLISHER": publisher,
                       "PUBLISHER_DISPLAY_NAME": publisher_display_name, "VERSION": version}.items():
        manifest = manifest.replace("{{" + key + "}}", escape(value, {'"': "&quot;"}))
    ET.fromstring(manifest)
    (layout / "AppxManifest.xml").write_text(manifest, encoding="utf-8")
    return layout


def find_makeappx():
    found = shutil.which("makeappx.exe")
    if found:
        return found
    sdk = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")) / "Windows Kits/10/bin"
    candidates = list(sdk.glob("*/x64/makeappx.exe"))
    if not candidates:
        raise FileNotFoundError("makeappx.exe not found; install the Windows 10/11 SDK")
    return str(max(candidates, key=lambda p: tuple(int(n) for n in p.parts[-3].split("."))))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="dist/TranscribeApp")
    parser.add_argument("--layout", default="build/msix-layout")
    parser.add_argument("--output", default="Gusan-Windows-Store.msix")
    parser.add_argument("--version", required=True)
    parser.add_argument("--identity-name", default=os.getenv("MSIX_IDENTITY_NAME") or "AIBUBEN.Gusan")
    parser.add_argument("--publisher", default=os.getenv("MSIX_PUBLISHER") or "CN=00000000-0000-0000-0000-000000000000")
    parser.add_argument("--publisher-display-name", default=os.getenv("MSIX_PUBLISHER_DISPLAY_NAME") or "AIBUBEN")
    parser.add_argument("--layout-only", action="store_true")
    args = parser.parse_args()
    layout = generate_layout(args.source, args.layout, args.version, args.identity_name,
                             args.publisher, args.publisher_display_name)
    print("Created layout: " + str(layout))
    if args.layout_only:
        return
    if sys.platform != "win32":
        parser.error("Packing requires Windows; use --layout-only on other platforms")
    output = Path(args.output).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run([find_makeappx(), "pack", "/d", str(layout), "/p", str(output), "/o"], check=True)
    print("Created " + str(output))


if __name__ == "__main__":
    main()
