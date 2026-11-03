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
PLAYER = INSTALLER / "player"
VIDEO = WIZARD / "gusan_intro.mp4"

MAX_WIZARD_BYTES = 4_000_000          # all pictures together, as stored in [Files]
MAX_VIDEO_BYTES = 8_000_000           # the intro video (installer/make_wizard_video.py)

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
        if not m:                           # a preprocessor line (#if HaveVideo ... #endif)
            continue
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
        # The pictures only: the video (and the player built next to it) has a budget of its own.
        total = sum(p.stat().st_size for p in WIZARD.rglob("*.png"))
        self.assertLess(total, MAX_WIZARD_BYTES, "the pictures in installer/wizard are %.2f MB" % (total / 1e6))


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
        # Solid compression: Setup unpacks everything above a file it extracts. The video and its
        # player (2 files) come first, then the pictures, then the program files.
        self.assertEqual([i for i, (src, _f) in enumerate(sources) if src.startswith("..")],
                         [2 + len(frames)])
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

    def test_the_timer_declarations_are_the_ones_from_the_inno_setup_6_example(self):
        # Examples\CodeDll.iss of Inno Setup 6.7: plain LongWord. HWND, UINT_PTR and NativeInt
        # are types of Inno Setup 7; the Inno Setup 6 compiler (CI) does not know them.
        self.assertIn("function SetTimer(hWnd, nIDEvent, uElapse, lpTimerFunc: LongWord): LongWord;\n"
                      "  external 'SetTimer@user32.dll stdcall';", self.text)
        self.assertIn("function KillTimer(hWnd, nIDEvent: LongWord): BOOL;\n"
                      "  external 'KillTimer@user32.dll stdcall';", self.text)
        self.assertIn("procedure SlideTimerProc(Arg1, Arg2, Arg3, Arg4: LongWord);", self.text)
        self.assertIn("  SlideTimer: LongWord;", self.text)
        self.assertIn("  TimerId: LongWord;", self.text)
        self.assertEqual(self.text.count("CreateCallback(@SlideTimerProc)"), 1)
        # self.code has no comments: the comment above the declarations names these types on purpose.
        for seven_only in ("NativeInt", "NativeUInt", "UINT_PTR", "HWND"):
            self.assertNotIn(seven_only, self.code)

    def test_nothing_of_the_slideshow_runs_in_a_silent_install(self):
        body = self._body("StartSlides")
        guard = re.search(r"if\s+WizardSilent\s+then\s+Exit;", body)
        self.assertTrue(guard, "StartSlides must leave at once when WizardSilent")
        # Everything that has an effect comes after the guard ...
        for call in ("ExtractTemporaryFile", "TBitmapImage.Create", "SetTimer(", "CreateCallback", ".Parent"):
            at = body.find(call)
            self.assertGreater(at, guard.end(), call)
        # ... and only StartSlides does those things (StartVideo extracts the video, with a guard of its own).
        for name, other in self.routines.items():
            if name in ("StartSlides", "StartVideo", "SetTimer", "KillTimer"):
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
        self.assertRegex(step, r"if\s+CurStep\s*=\s*ssPostInstall\s+then\s+begin\s+StopSlides;\s+StopVideo;\s+end;")
        self.assertLess(step.index("StopSlides"), step.index("SaveStringToFile"))
        # ... the next page (the finish page) ...
        page = self._body("CurPageChanged")
        self.assertLess(page.index("else"), page.index("StopSlides"))
        # ... and Setup ending without either of them (Cancel, an error).
        self.assertRegex(self._body("DeinitializeSetup"), r"begin\s+StopSlides;\s+StopVideo;\s+end;")

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
        box = self._body("SlideBox")           # shared by the video and the pictures
        for live in ("WizardForm.ProgressGauge.Width", "WizardForm.ProgressGauge.Top",
                     "WizardForm.ProgressGauge.Height", "WizardForm.InstallingPage.Height"):
            self.assertIn(live, box)
        self.assertIn("Height := Width * 2 div 5;", box)
        self.assertIn("Width := Height * 5 div 2;", box)
        body = self._body("StartSlides")
        self.assertIn("SlideBox(", body)
        self.assertIn("Stretch := True", body)
        # Set up before it is shown: Parent is the last property set.
        self.assertGreater(body.index(".Parent :="), body.index(".Height :="))
        # No hard-coded design size (the wizard is 120 percent of it).
        for part in (box, body):
            self.assertNotIn("ScaleX(", part)
            self.assertNotIn("417", part)

    def test_pictures_are_loaded_one_at_a_time_from_the_temporary_folder(self):
        show = self._body("ShowSlide")
        self.assertIn("SlideImage.PngImage.LoadFromFile(", show)
        self.assertIn("ExpandConstant('{tmp}')", _read(ISS))
        self.assertNotIn("Bitmap.LoadFromFile", self.code)        # that one is for .bmp files
        self.assertEqual(len(re.findall(r"\bLoadFromFile\b", self.code)), 1)


class TestVideo(unittest.TestCase):
    """The intro video of the Installing page and the program that plays it.

    A slideshow that Setup draws itself moves about once a second (its timer only runs between two files), so the
    video is played by a program of its own. Not testable here: that the player really plays (it was run against
    a real Setup window by hand) and how it looks at each screen scale."""

    def setUp(self):
        self.text = _read(ISS)
        self.code = _code(self.text)
        self.routines = _routines(self.code)
        self.cs = _read(PLAYER / "GusanSlides.cs")

    def _body(self, name):
        return self.routines[name]

    def test_the_video_is_a_1000_by_400_h264_mp4_that_stays_small(self):
        data = VIDEO.read_bytes()
        self.assertEqual(data[4:8], b"ftyp")
        self.assertLess(len(data), MAX_VIDEO_BYTES)
        head = data[:65536]
        self.assertIn(b"moov", head)                 # the header comes first: it starts at once
        at = head.index(b"avc1", head.index(b"stsd"))   # H.264 (not the brand in ftyp): Windows plays it everywhere
        self.assertEqual(struct.unpack(">HH", head[at + 4 + 24:at + 4 + 28]), (1000, 400))

    def test_the_sources_and_the_build_script_are_there(self):
        for name in ("GusanSlides.cs", "app.manifest", "build.ps1"):
            self.assertTrue((PLAYER / name).is_file(), name)
        build = _read(PLAYER / "build.ps1")
        self.assertIn("csc.exe", build)
        self.assertIn("wizard\\GusanSlides.exe", build)

    def test_the_script_includes_the_video_only_when_both_files_exist(self):
        self.assertIn("#define HaveVideo 0", self.text)
        self.assertEqual(len(re.findall(r'#ifexist AddBackslash\(SourcePath\) \+ "wizard\\', self.text)), 2)
        files = re.search(r"(?s)\[Files\](.*?)\[Icons\]", self.text).group(1)
        self.assertRegex(files, r'(?s)#if HaveVideo\s+Source: "wizard\\gusan_intro\.mp4".*?\s+'
                                r'Source: "wizard\\GusanSlides\.exe".*?\s+#endif')
        sources = dict(_file_sources(self.text))
        for name in ("wizard\\gusan_intro.mp4", "wizard\\GusanSlides.exe"):
            self.assertIn("dontcopy", sources[name], name)       # compiled in, never installed
            self.assertIn("noencryption", sources[name], name)
        self.assertIn("nocompression", sources["wizard\\gusan_intro.mp4"])   # mp4 is compressed already
        names = [src for src, _f in _file_sources(self.text)]
        self.assertEqual(names[:2], ["wizard\\gusan_intro.mp4", "wizard\\GusanSlides.exe"])

    def test_the_video_is_started_once_and_never_in_a_silent_install(self):
        body = self._body("StartVideo")
        guard = re.search(r"if\s+WizardSilent\s+or\s+VideoLaunched\s+then\s+Exit;\s+VideoLaunched\s*:=\s*True;", body)
        self.assertTrue(guard, "StartVideo must leave at once when silent or already tried, and mark the try")
        start, stop = body.index("try"), body.index("except")
        self.assertGreater(start, guard.end())
        for call in ("SlideBox(", "ExtractTemporaryFile", "Exec("):
            self.assertTrue(start < body.index(call) < stop, call)
        # An error just means: no video (the pictures are shown).
        self.assertRegex(body[stop:], r"(?s)except\s+Log\(''\s*\+\s*GetExceptionMessage\);\s+end;\s+end;")
        self.assertIn("ewNoWait", self.text)
        self.assertEqual(len(re.findall(r"\bStartVideo\b", self.code)), 2)      # the routine and one call
        self.assertRegex(self._body("InitializeWizard"), r"StartVideo;\s+end;")

    def test_setup_and_the_player_agree_on_the_arguments_and_the_window_titles(self):
        for arg in ("--owner", "--page", "--x", "--y", "--w", "--h", "--video"):
            self.assertIn(arg + " ", self.text)
            self.assertIn('"%s"' % arg, self.cs)
        titles = re.findall(r"VideoTitle\w+\s*=\s*'(\w+)'", self.text)
        self.assertEqual(len(titles), 2)
        for title in titles:
            self.assertIn('"%s"' % title, self.cs)

    def test_the_video_stops_on_every_way_out(self):
        self.assertIn("PostMessageW(H, $0010, 0, 0)", self.text)      # WM_CLOSE
        self.assertRegex(self._body("CurStepChanged"), r"ssPostInstall\s+then\s+begin\s+StopSlides;\s+StopVideo;")
        self.assertRegex(self._body("DeinitializeSetup"), r"StopVideo;\s+end;")
        # It waits until the player is gone: Setup deletes {tmp}, where the player is, when it ends.
        self.assertIn("Sleep(50)", self.text)

    def test_the_pictures_are_only_the_fallback(self):
        start = self._body("StartSlides")
        self.assertRegex(start, r"if\s+VideoPlaying\s+then\s+Exit;")
        # The video came up late: the pictures stop and it takes over.
        tick = self._body("SlideTimerProc")
        self.assertRegex(tick, r"(?s)if\s+VideoPlaying\s+then\s+begin\s+StopSlides;\s+Exit;")

    def test_the_player_never_takes_focus_ends_with_setup_and_stays_offline(self):
        for part in ("WS_EX_NOACTIVATE", "WS_EX_TOOLWINDOW", "HTTRANSPARENT", "ShowInTaskbar = false",
                     "ShowActivated = false", "IsWindow(opt.Owner)", "Application.Current.Shutdown"):
            self.assertIn(part, self.cs)
        self.assertNotIn("System.Net", self.cs)
        self.assertNotIn("Process.Start", self.cs)

    def test_the_release_build_makes_the_player_before_the_installer(self):
        workflow = _read(ROOT / ".github" / "workflows" / "build.yml")
        self.assertIn("./installer/player/build.ps1", workflow)
        self.assertLess(workflow.index("./installer/player/build.ps1"), workflow.index("installer\\installer.iss"))
        self.assertIn("installer/wizard/GusanSlides.exe", _read(ROOT / ".gitignore"))


class TestFinishPage(unittest.TestCase):

    def setUp(self):
        self.text = _read(ISS)

    def test_the_finish_page_text_is_short_and_says_gusan(self):
        msgs = dict(line.split("=", 1) for line in _section(self.text, "Messages"))
        self.assertEqual(sorted(msgs), ["FinishedHeadingLabel", "FinishedLabel", "FinishedLabelNoIcons"])
        self.assertEqual(msgs["FinishedHeadingLabel"], "{#MyAppName} is ready")
        self.assertEqual(msgs["FinishedLabel"], msgs["FinishedLabelNoIcons"])
        self.assertEqual(msgs["FinishedLabel"], "Press your dictation shortcut in any app and talk.")

    def test_the_finish_text_names_no_key(self):
        # The shortcut can be changed in the app (or be a mouse button): never promise one.
        for line in _section(self.text, "Messages"):
            self.assertNotRegex(line, r"(?i)\b(alt|ctrl|shift|win)\b|\bF\d{1,2}\b", line)

    def test_an_update_says_updated_in_the_one_line_heading(self):
        page = _routines(_code(self.text))["CurPageChanged"]
        self.assertRegex(page, r"if\s+\(CurPageID\s*=\s*wpFinished\)\s+and\s+IsUpgrade\s+then")
        self.assertIn("WizardForm.FinishedHeadingLabel.Caption := ", page)
        self.assertIn("'{#MyAppName} is updated'", self.text)

    def test_the_launch_box_still_says_launch_gusan(self):
        # The in-app update added a second [Run] entry (the silent /relaunch one): the Launch checkbox
        # is the one that has a Description.
        run = [line for line in _section(self.text, "Run") if "Description:" in line]
        self.assertEqual(len(run), 1)
        self.assertIn('Description: "Launch {#MyAppName}"', run[0])
        self.assertIn("postinstall", run[0])
        self.assertIn("skipifsilent", run[0])


class TestPictureCopy(unittest.TestCase):
    """The words in the pictures are baked into PNG files, so they are checked in the script that draws them
    (installer/make_wizard_art.py) and in the manifest it writes."""

    def setUp(self):
        self.art = _read(INSTALLER / "make_wizard_art.py")
        self.manifest = _read(WIZARD / "slides.json")

    def test_no_retired_wording_in_the_pictures(self):
        # "interview" is a retired word (Live Assistant is not for interviews); sample files are team calls.
        for name, body in (("make_wizard_art.py", self.art), ("slides.json", self.manifest)):
            self.assertNotRegex(body, r"(?i)interview", name)

    def test_the_finish_picture_names_no_key(self):
        start = self.art.index("def finish_panel(")
        body = self.art[start:self.art.index("def small_image(")]
        self.assertNotIn('"Alt"', body)
        self.assertNotIn("keys(", body)

    def test_smart_actions_is_not_the_dictation_scene(self):
        # Slide 1 is an email to Anna, slide 2 a chat with Sam: two different pictures of two different things.
        slides = {s["n"]: s for s in json.loads(self.manifest)["slides"]}
        self.assertEqual(slides[2]["title"], "Say it, get it written")
        self.assertEqual(slides[2]["line"], "Say it roughly. Get it written properly.")
        scene2 = self.art[self.art.index("def scene2("):self.art.index("# ── slide 3")]
        self.assertNotIn("mail_window", scene2)
        self.assertNotIn("Anna", scene2)


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
