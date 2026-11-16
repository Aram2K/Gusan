"""installer/trim_bundle.py: the parts of the Windows bundle Gusan never uses are removed after PyInstaller (2026-11-16
size audit: about 28 MB of the installer) - and never one that a file that stays still links to."""
import os
import re
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "installer"))
import trim_bundle as tb  # noqa: E402


def _tree(root, files):
    for rel in files:
        p = Path(root, "_internal", rel)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"x" * 10)


class TestPlan(unittest.TestCase):
    FILES = ("PySide6/Qt6Core.dll", "PySide6/Qt6Quick.dll", "PySide6/Qt6QmlModels.dll", "PySide6/Qt6Pdf.dll",
             "PySide6/avcodec-61.dll", "PySide6/translations/qtbase_fr.qm",
             "PySide6/plugins/multimedia/windowsmediaplugin.dll", "PySide6/plugins/multimedia/ffmpegmediaplugin.dll",
             "PySide6/plugins/platforms/qwindows.dll", "PySide6/plugins/platforms/qdirect2d.dll",
             "PySide6/Qt6Multimedia.dll", "av.libs/avcodec-62.dll", "pytest/__init__.py", "numpy/core.pyd")

    def test_only_the_unused_parts_and_never_what_the_app_runs_on(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tree(tmp, self.FILES)
            gone = {os.path.relpath(p, os.path.join(tmp, "_internal")).replace(os.sep, "/")
                    for p, _s in tb.plan(tmp)}
        self.assertEqual(gone, {"PySide6/Qt6Quick.dll", "PySide6/Qt6QmlModels.dll", "PySide6/Qt6Pdf.dll",
                                "PySide6/avcodec-61.dll", "PySide6/translations",
                                "PySide6/plugins/multimedia/ffmpegmediaplugin.dll",
                                "PySide6/plugins/platforms/qdirect2d.dll", "pytest"})
        for keep in tb.KEEP:                          # (the app's own Qt, its media plugin, its platform)
            self.assertNotIn(keep, gone)
        self.assertNotIn("av.libs/avcodec-62.dll", gone)        # PyAV's own FFmpeg stays: Audio to text reads with it

    def test_nothing_goes_when_a_file_that_stays_still_links_to_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            _tree(tmp, self.FILES)
            links = {"Qt6Multimedia.dll": {"qt6core.dll", "qt6pdf.dll"}}

            def imports(path):
                return links.get(os.path.basename(path), set())
            # (the links are mocked; pefile itself comes with PyInstaller, which the Linux test job has not got)
            with mock.patch.object(tb, "_imports", imports), \
                    mock.patch.dict(sys.modules, {"pefile": types.ModuleType("pefile")}):
                broken = tb.still_needed(tmp, tb.plan(tmp))
                self.assertEqual(broken, [("_internal/PySide6/Qt6Multimedia.dll", "qt6pdf.dll")])
                if sys.platform == "win32":
                    self.assertEqual(tb.main([tmp]), 2)
                    self.assertTrue(Path(tmp, "_internal", "PySide6", "Qt6Pdf.dll").exists())      # removed nothing
                links.clear()
                self.assertEqual(tb.still_needed(tmp, tb.plan(tmp)), [])
                self.assertEqual(tb.main([tmp, "--check"]), 0)
                self.assertTrue(Path(tmp, "_internal", "PySide6", "Qt6Pdf.dll").exists())          # (--check)
                if sys.platform == "win32":
                    self.assertEqual(tb.main([tmp]), 0)
                    self.assertFalse(Path(tmp, "_internal", "PySide6", "Qt6Pdf.dll").exists())
                    self.assertFalse(Path(tmp, "_internal", "PySide6", "translations").exists())
                    self.assertTrue(Path(tmp, "_internal", "PySide6", "Qt6Core.dll").exists())


class TestTheBuildsRunIt(unittest.TestCase):
    def test_both_windows_builds_trim_after_the_media_check_and_before_the_zip(self):
        yml = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        ps1 = (ROOT / "build.ps1").read_text(encoding="utf-8")
        for text in (yml, ps1):
            check = text.index("# multimedia-check:end")
            trim = text.index("installer/trim_bundle.py dist\\TranscribeApp")
            self.assertLess(check, trim)
            self.assertIn("if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }", text[trim:trim + 200])
        self.assertLess(yml.index("installer/trim_bundle.py"), yml.index("Compress-Archive"))
        self.assertLess(yml.index("installer/trim_bundle.py"), yml.index("installer\\installer.iss"))

    def test_the_test_tools_are_left_out_of_every_build(self):
        yml = (ROOT / ".github" / "workflows" / "build.yml").read_text(encoding="utf-8")
        ps1 = (ROOT / "build.ps1").read_text(encoding="utf-8").replace('"', "")
        for mod in ("pytest", "_pytest", "pygments"):
            self.assertEqual(len(re.findall(r"--exclude-module %s\b" % re.escape(mod), yml)), 2, mod)   # Windows, Mac
            self.assertRegex(ps1, r"--exclude-module %s\b" % re.escape(mod))


if __name__ == "__main__":
    unittest.main()
