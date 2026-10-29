"""Guards for the installer's slideshow and wizard pictures.

installer/installer.iss shows a looping slideshow under the progress bar while
Setup copies the files (an in-app update runs the same wizard), and brands the
finish page. There is no Inno Setup compiler on a developer PC and CI compiles
the script only when a release is built, so a mistake in the Pascal Script
would show up as a broken release. These tests read the script and the pictures
as plain files (no Qt, no Pillow, no Inno Setup) and pin what can be pinned:

* every picture the script names exists, has the size Inno Setup asks for, and
  the whole folder stays small;
* the frame list in [Code] is the one in installer/wizard/slides.json, and each
  frame is in [Files] (dontcopy, before the 100 MB of program files);
* nothing of the slideshow runs in a silent install, the timer is only started
  in one place and stopped on every way out, and the timer callback cannot raise;
* the Pascal blocks balance and every routine is declared before it is used
  (Pascal Script has no forward lookup);
* what existing installs depend on keeps its name.

Not testable here (see the report that came with this change): that the script
compiles, how the Installing page really looks at each screen scale, and that
the timer ticks while Setup copies files.
"""

import json
import re
import struct
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALLER = ROOT / "installer"
ISS = INSTALLER / "installer.iss"
WIZARD = INSTALLER / "wizard"

MAX_WIZARD_BYTES = 4_000_000          # all pictures together, as stored in [Files]

# Inno Setup 6.6+ image areas at 100 / 125 / 150 / 175 / 200 / 225 / 250 percent
# (https://jrsoftware.org/ishelp/topic_setup_wizardimagefile.htm and
# topic_setup_wizardsmallimagefile.htm). Setup picks the closest listed file.
LARGE_AREAS = [(202, 386), (269, 515), (336, 643), (403, 772), (430, 824), (498, 953), (534, 1022)]
SMALL_AREAS = [58, 77, 97, 116, 124, 143, 159]

DIST_LINE = ('Source: "..\\dist\\TranscribeApp\\*"; DestDir: "{app}"; '
             'Flags: recursesubdirs createallsubdirs ignoreversion')


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _png_size(path):
    """(width, height) from the IHDR chunk of a PNG file."""
    with open(path, "rb") as f:
        head = f.read(24)
    assert head[:8] == b"\x89PNG\r\n\x1a\n", "%s is not a PNG file" % path
    assert head[12:16] == b"IHDR", "%s has no IHDR chunk first" % path
    return struct.unpack(">II", head[16:24])


def _section(text, name):
    """The non-comment, non-blank lines of one [Section] (not [Code])."""
    lines, inside = [], False
    for raw in text.splitlines():
        line = raw.strip()
        if re.fullmatch(r"\[[A-Za-z]+\]", line):
            inside = line.lower() == "[%s]" % name.lower()
            continue
        if inside and line and not line.startswith(";"):
            lines.append(line)
    return lines


def _setup(text):
    out = {}
    for line in _section(text, "Setup"):
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out


def _file_sources(text):
    """[Files] lines as (source, flags) in file order."""
    out = []
    for line in _section(text, "Files"):
        m = re.match(r'Source:\s*"([^"]+)"', line)
        flags = re.search(r"Flags:\s*([^;]*)", line)
        out.append((m.group(1), flags.group(1).split() if flags else []))
    return out


def _strip_pascal(code):
    """Pascal Script without strings and // comments (so words in them don't count)."""
    # One pass, left to right: an apostrophe inside a comment is not a string,
    # and '//' inside a string is not a comment.
    return re.sub(r"'(?:[^'\n]|'')*'|//[^\n]*",
                  lambda m: "''" if m.group(0).startswith("'") else "", code)


def _code(text):
    """The [Code] section, with strings and comments removed."""
    head = re.search(r"^\[Code\]\s*$", text, re.M)
    assert head, "no [Code] section"
    return _strip_pascal(text[head.end():])


def _routines(code):
    """Ordered {name: text} of the procedures and functions of a [Code] section.

    A routine starts at a line that begins with 'procedure' or 'function' and
    runs up to the next one. The text includes its own header line."""
    out, name, buf = {}, None, []
    for line in code.splitlines():
        m = re.match(r"(?:procedure|function)\s+(\w+)", line, re.I)
        if m:
            if name:
                out[name] = "\n".join(buf)
            name, buf = m.group(1), []
        if name:
            buf.append(line)
    if name:
        out[name] = "\n".join(buf)
    return out


def _words(text):
    return re.findall(r"[A-Za-z_]\w*", text)


class TestPictures(unittest.TestCase):

    def setUp(self):
        self.text = _read(ISS)
        self.setup = _setup(self.text)

    def _listed(self, key):
        value = self.setup[key]
        self.assertNotIn(" ", value, "%s: no spaces around the commas" % key)
        return value.split(",")

    def test_every_picture_the_script_names_exists(self):
        names = self._listed("WizardImageFile") + self._listed("WizardSmallImageFile")
        names += [src for src, _flags in _file_sources(self.text) if not src.startswith("..")]
        self.assertGreater(len(names), 40)
        for name in names:
            self.assertTrue((INSTALLER / name).is_file(), name)

    def test_the_script_needs_inno_setup_6_5_2_for_png(self):
        self.assertRegex(self.text, r"(?m)^#if VER < EncodeVer\(6,5,2\)\s*\n\s*#error [^\n]+\n#endif")
        for name in self._listed("WizardImageFile") + self._listed("WizardSmallImageFile"):
            self.assertTrue(name.endswith(".png"), name)

    def test_the_finish_page_picture_has_a_file_for_every_screen_scale(self):
        names = self._listed("WizardImageFile")
        sizes = []
        for name in names:
            m = re.fullmatch(r"wizard\\finish_(\d+)x(\d+)\.png", name)
            self.assertTrue(m, name)
            w, h = int(m.group(1)), int(m.group(2))
            self.assertEqual(_png_size(INSTALLER / name), (w, h), name)
            self.assertAlmostEqual(w / h, 164 / 314, delta=0.005, msg=name)    # Inno's ratio
            sizes.append((w, h))
        for area in LARGE_AREAS:
            self.assertIn(area, sizes)

    def test_the_header_logo_is_square_with_a_file_for_every_screen_scale(self):
        sizes = []
        for name in self._listed("WizardSmallImageFile"):
            m = re.fullmatch(r"wizard\\small_(\d+)\.png", name)
            self.assertTrue(m, name)
            side = int(m.group(1))
            self.assertEqual(_png_size(INSTALLER / name), (side, side), name)
            sizes.append(side)
        for side in SMALL_AREAS:
            self.assertIn(side, sizes)

    def test_slides_are_1000_by_400(self):
        # The control is made 5:2 in the script (ProgressGauge width * 2 / 5).
        for path in sorted(WIZARD.glob("slide*.png")):
            self.assertEqual(_png_size(path), (1000, 400), path.name)

    def test_all_pictures_together_stay_small(self):
        total = sum(p.stat().st_size for p in WIZARD.rglob("*") if p.is_file())
        self.assertLess(total, MAX_WIZARD_BYTES, "installer/wizard is %.2f MB" % (total / 1e6))


class TestFrameList(unittest.TestCase):

    def setUp(self):
        self.text = _read(ISS)
        self.slides = json.loads(_read(WIZARD / "slides.json"))
        self.frames = [f for s in self.slides["slides"] for f in s["frames"]]
        self.added = re.findall(r"AddSlide\('([^']+)',\s*(\d+)\)", self.text)

    def test_the_tick_is_100_ms_and_every_frame_time_is_a_whole_number_of_ticks(self):
        self.assertEqual(self.slides["tick_ms"], 100)
        self.assertIn("SetTimer(0, 0, 100,", self.text)
        for f in self.frames:
            self.assertEqual(f["ms"] % 100, 0, f)
            self.assertGreaterEqual(f["ms"], 300, f)

    def test_the_frame_list_in_code_is_slides_json(self):
        expected = [(f["file"], str(f["ms"] // 100)) for f in self.frames]
        self.assertEqual(self.added, expected)

    def test_every_frame_is_in_files_as_dontcopy_before_the_program_files(self):
        sources = _file_sources(self.text)
        frames = [(src, flags) for src, flags in sources if src.startswith("wizard\\slide")]
        self.assertEqual([src[len("wizard\\"):] for src, _ in frames], [f["file"] for f in self.frames])
        for src, flags in frames:
            self.assertIn("dontcopy", flags, src)
            self.assertIn("noencryption", flags, src)
        # Solid compression: Setup unpacks everything above a file it extracts.
        self.assertEqual([i for i, (src, _f) in enumerate(sources) if src.startswith("..")],
                         [len(frames)])
        self.assertIn("solidbreak", frames[0][1])

    def test_the_program_files_line_is_unchanged(self):
        self.assertIn(DIST_LINE, _section(self.text, "Files"))

    def test_the_slides_have_their_copy_in_the_pictures_only(self):
        # The copy is baked into the pictures: the script has no caption labels.
        self.assertNotIn("TNewStaticText.Create", self.text)
        self.assertNotIn("TLabel", self.text)


class TestSlideshowCode(unittest.TestCase):

    def setUp(self):
        self.text = _read(ISS)
        self.code = _code(self.text)
        self.routines = _routines(self.code)

    def _body(self, name):
        return self.routines[name]

    def test_pascal_blocks_balance(self):
        for name, body in self.routines.items():
            if "external" in body.lower() and "begin" not in body.lower():
                continue
            words = [w.lower() for w in _words(body)]
            opened = sum(words.count(w) for w in ("begin", "try", "case"))
            self.assertEqual(opened, words.count("end"), name)

    def test_every_routine_is_declared_before_it_is_used(self):
        names = list(self.routines)
        for index, name in enumerate(names):
            used = set(_words(self._body(name)))
            for other in names:
                if other != name and other in used:
                    self.assertLess(names.index(other), index,
                                    "%s uses %s, which is declared after it" % (name, other))

    def test_the_timer_declarations_are_the_ones_from_the_inno_setup_example(self):
        # Examples\CodeDll.iss of Inno Setup.
        self.assertIn("function SetTimer(hWnd: HWND; nIDEvent: UINT_PTR; uElapse: UINT; "
                      "lpTimerFunc: NativeInt): UINT_PTR;\n  external 'SetTimer@user32.dll stdcall';", self.text)
        self.assertIn("function KillTimer(hWnd: HWND; nIDEvent: UINT_PTR): BOOL;\n"
                      "  external 'KillTimer@user32.dll stdcall';", self.text)
        self.assertIn("procedure SlideTimerProc(Arg1: HWND; Arg2: UINT; Arg3: UINT_PTR; Arg4: DWORD);", self.text)
        self.assertEqual(self.text.count("CreateCallback(@SlideTimerProc)"), 1)

    def test_nothing_of_the_slideshow_runs_in_a_silent_install(self):
        body = self._body("StartSlides")
        guard = re.search(r"if\s+WizardSilent\s+then\s+Exit;", body)
        self.assertTrue(guard, "StartSlides must leave at once when WizardSilent")
        # Everything that has an effect comes after the guard ...
        for call in ("ExtractTemporaryFile", "TBitmapImage.Create", "SetTimer(", "CreateCallback", ".Parent"):
            at = body.find(call)
            self.assertGreater(at, guard.end(), call)
        # ... and only StartSlides does those things.
        for name, other in self.routines.items():
            if name in ("StartSlides", "SetTimer", "KillTimer"):
                continue
            for call in ("ExtractTemporaryFile", "TBitmapImage.Create", "SetTimer(", "CreateCallback"):
                self.assertNotIn(call, other, "%s must not call %s" % (name, call))
        # The only way in is the Installing page.
        page = self._body("CurPageChanged")
        self.assertRegex(page, r"if\s+CurPageID\s*=\s*wpInstalling\s+then\s+StartSlides\s+else")
        self.assertEqual(len(re.findall(r"\bStartSlides\b", self.code)), 2)       # the routine and that one call

    def test_the_timer_is_started_in_one_place_and_killed_in_one_place(self):
        self.assertEqual(len(re.findall(r"\bSetTimer\s*\(", self.code)), 2)       # the declaration and the call
        self.assertEqual(len(re.findall(r"\bKillTimer\s*\(", self.code)), 2)      # the declaration and the call
        self.assertIn("KillTimer(0, TimerId)", self._body("StopSlideTimer"))
        # The id is cleared before the call: a message already queued then finds 0.
        stop = self._body("StopSlideTimer")
        self.assertLess(stop.index("SlideTimer := 0"), stop.index("KillTimer(0"))
        self.assertIn("StopSlideTimer", self._body("StopSlides"))

    def test_the_slideshow_stops_on_every_way_out(self):
        # The end of the install ...
        step = self._body("CurStepChanged")
        self.assertRegex(step, r"if\s+CurStep\s*=\s*ssPostInstall\s+then\s+StopSlides;")
        self.assertLess(step.index("StopSlides"), step.index("SaveStringToFile"))
        # ... the next page (the finish page) ...
        page = self._body("CurPageChanged")
        self.assertLess(page.index("else"), page.index("StopSlides"))
        # ... and Setup ending without either of them (Cancel, an error).
        self.assertRegex(self._body("DeinitializeSetup"), r"begin\s+StopSlides;\s+end;")

    def test_stopping_never_raises_and_releases_the_picture(self):
        body = self._body("StopSlides")
        self.assertRegex(body, r"(?s)try\s+\w+\.Free;\s+except\b.*\bend;")
        self.assertIn("SlideImage := nil", body)
        # Cleared before it is freed, so the callback can't reach a freed control.
        self.assertLess(body.index("SlideImage := nil"), body.index(".Free"))

    def test_the_timer_callback_cannot_raise(self):
        body = self._body("SlideTimerProc")
        guard = re.search(r"if\s+\(SlideTimer\s*=\s*0\)\s+or\s+\(SlideImage\s*=\s*nil\)\s+or\s+"
                          r"\(WizardForm\s*=\s*nil\)\s+then\s+Exit;", body)
        self.assertTrue(guard)
        start, mid, stop = body.index("try"), body.index("ShowSlide("), body.index("except")
        self.assertTrue(guard.end() <= start < mid < stop)
        self.assertRegex(body[stop:], r"(?s)except\s+StopSlideTimer;\s+end;\s+end;")
        # Nothing after the try block: everything that can fail is inside it.
        self.assertRegex(body, r"(?s)end;\s+end;\s*$")

    def test_starting_never_raises_and_a_failure_means_no_slideshow(self):
        body = self._body("StartSlides")
        start, stop = body.index("try"), body.index("except")
        for call in ("ExtractTemporaryFile", "TBitmapImage.Create", "SetTimer(", ".PngImage", "ShowSlide(0)"):
            if call in body:
                self.assertTrue(start < body.index(call) < stop, call)
        self.assertRegex(body[stop:], r"(?s)except\s+Log\(''\s*\+\s*GetExceptionMessage\);\s+StopSlides;\s+end;")

    def test_the_picture_is_sized_from_the_live_controls_and_is_5_to_2(self):
        body = self._body("StartSlides")
        for live in ("WizardForm.ProgressGauge.Width", "WizardForm.ProgressGauge.Top",
                     "WizardForm.ProgressGauge.Height", "WizardForm.InstallingPage.Height"):
            self.assertIn(live, body)
        self.assertIn("H := W * 2 div 5;", body)
        self.assertIn("W := H * 5 div 2;", body)
        self.assertIn("Stretch := True", body)
        # Set up before it is shown: Parent is the last property set.
        self.assertGreater(body.index(".Parent :="), body.index(".Height :="))
        # No hard-coded design size (the wizard is 120 percent of it).
        self.assertNotIn("ScaleX(", body)
        self.assertNotIn("417", body)

    def test_pictures_are_loaded_one_at_a_time_from_the_temporary_folder(self):
        show = self._body("ShowSlide")
        self.assertIn("SlideImage.PngImage.LoadFromFile(", show)
        self.assertIn("ExpandConstant('{tmp}')", _read(ISS))
        self.assertNotIn("Bitmap.LoadFromFile", self.code)        # that one is for .bmp files
        self.assertEqual(len(re.findall(r"\bLoadFromFile\b", self.code)), 1)


class TestFinishPage(unittest.TestCase):

    def setUp(self):
        self.text = _read(ISS)

    def test_the_finish_page_text_is_short_and_says_gusan(self):
        msgs = dict(line.split("=", 1) for line in _section(self.text, "Messages"))
        self.assertEqual(sorted(msgs), ["FinishedHeadingLabel", "FinishedLabel", "FinishedLabelNoIcons"])
        self.assertEqual(msgs["FinishedHeadingLabel"], "{#MyAppName} is ready")
        self.assertEqual(msgs["FinishedLabel"], msgs["FinishedLabelNoIcons"])
        self.assertEqual(msgs["FinishedLabel"], "Press Alt + R in any app and talk.")

    def test_the_default_shortcut_in_the_text_is_the_apps_default(self):
        main = _read(ROOT / "main.py")
        self.assertRegex(main, r'"hotkey":\s*"alt\+r"')

    def test_an_update_says_updated_in_the_one_line_heading(self):
        page = _routines(_code(self.text))["CurPageChanged"]
        self.assertRegex(page, r"if\s+\(CurPageID\s*=\s*wpFinished\)\s+and\s+IsUpgrade\s+then")
        self.assertIn("WizardForm.FinishedHeadingLabel.Caption := ", page)
        self.assertIn("'{#MyAppName} is updated'", self.text)

    def test_the_launch_box_still_says_launch_gusan(self):
        run = _section(self.text, "Run")
        self.assertEqual(len(run), 1)
        self.assertIn('Description: "Launch {#MyAppName}"', run[0])
        self.assertIn("postinstall", run[0])
        self.assertIn("skipifsilent", run[0])


class TestNamesExistingInstallsDependOn(unittest.TestCase):
    """The same things tests/test_release_2_0_0.py pins, seen from this change:
    the slideshow added lines and left every existing one as it was."""

    def setUp(self):
        self.text = _read(ISS)
        self.setup = _setup(self.text)

    def test_identity_and_output_names(self):
        self.assertEqual(self.setup["AppId"], "{{8E3B7C8A-9D54-4F61-9F6C-2E8C7F0A1B23}")
        self.assertEqual(self.setup["OutputBaseFilename"], "TranscribeApp-Setup")
        self.assertIn('#define MyAppExeName "TranscribeApp.exe"', self.text)
        self.assertIn('#define MyRunValueName "Transcribe"', self.text)
        self.assertEqual(self.setup["WizardStyle"], "modern")
        self.assertEqual(self.setup["DisableFinishedPage"], "no")
        self.assertNotIn("DisableWelcomePage", self.setup)       # no welcome page: no extra click

    def test_tasks_and_the_registry_run_value(self):
        names = [re.match(r'Name:\s*"(\w+)"', line).group(1) for line in _section(self.text, "Tasks")]
        self.assertEqual(names, ["desktopicon", "startup"])
        self.assertIn("Check: not IsUpgrade", _section(self.text, "Tasks")[1])
        reg = " ".join(_section(self.text, "Registry"))
        self.assertIn('ValueName: "{#MyRunValueName}"', reg)
        self.assertIn("Tasks: startup", reg)

    def test_install_delete_list_is_whole(self):
        self.assertEqual(len(_section(self.text, "InstallDelete")), 4)

    def test_upgrade_logic_and_the_analytics_page_are_still_there(self):
        routines = _routines(_code(self.text))
        for name in ("IsUpgrade", "FindPreviousInstall", "InitializeWizard", "CurStepChanged",
                     "CurUninstallStepChanged", "InitializeSetup"):
            self.assertIn(name, routines)
        self.assertIn("UpgradeInstall := FindPreviousInstall();", self.text)
        self.assertIn("AnalyticsPage := CreateInputOptionPage(", self.text)
        self.assertIn("analytics_consent.accepted", self.text)
        self.assertIn("analytics_consent.declined", self.text)
        self.assertIn("(CurStep = ssPostInstall) and (not IsUpgrade)", self.text)
        self.assertIn("(CurStep = ssPostInstall) and (not WizardSilent)", self.text)

    def test_the_build_still_names_the_installer_the_way_the_updater_expects(self):
        build = _read(ROOT / ".github" / "workflows" / "build.yml")
        self.assertIn("Move-Item installer\\TranscribeApp-Setup.exe TranscribeApp-Windows-Setup.exe", build)


if __name__ == "__main__":
    unittest.main()
