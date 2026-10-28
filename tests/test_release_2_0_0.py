"""Release guards for 2.0.0: Transcribe is renamed Gusan.

Everything people see says Gusan. Everything existing installs depend on keeps
its old name (redesign-plan section 9.5): the installer AppId, the data folder,
the keyring service, TranscribeApp.exe, the TranscribeApp-* download names,
the start-at-login Run value, the repo and the single-instance port.

No Qt and no `import main`: these tests read files as text, or import the small
Qt-free modules (app_info, storage, autostart, updater). So they run the same
in the stubbed full run, on their own, and in CI's Ubuntu test job.
"""

import ast
import os
import re
import subprocess
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
ISS = ROOT / "installer" / "installer.iss"
BUILD_YML = ROOT / ".github" / "workflows" / "build.yml"
BUILD_PS1 = ROOT / "build.ps1"
MAIN_PY = ROOT / "main.py"
APP_INFO_PY = ROOT / "app_info.py"

# installer/installer.iss as released in 1.9.4 (git show v1.9.4:installer/installer.iss).
V1_APP_ID = "{{8E3B7C8A-9D54-4F61-9F6C-2E8C7F0A1B23}"
V1_APP_NAME = "Transcribe"
INSTALLER_ASSET = "TranscribeApp-Windows-Setup.exe"


def _read(path):
    return Path(path).read_text(encoding="utf-8")


def _git_show(ref, rel_path):
    """A file as committed at ``ref`` (read-only), or None without git/history."""
    try:
        out = subprocess.run(["git", "show", "%s:%s" % (ref, rel_path)], cwd=str(ROOT),
                             capture_output=True, timeout=20)
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.decode("utf-8", "replace")


def _v1_installer():
    """The last 1.x installer script: the v1.9.4 tag, else HEAD while HEAD is
    still 1.x. None when neither is available (a shallow CI checkout)."""
    for ref in ("v1.9.4", "HEAD"):
        text = _git_show(ref, "installer/installer.iss")
        if text and _defines(text).get("MyAppName") == V1_APP_NAME:
            return text
    return None


# ── Inno Setup script helpers ───────────────────────────────────────────────

def _defines(text):
    return dict(re.findall(r'^#define\s+(\w+)\s+"([^"]*)"', text, re.M))


def _expand(value, defines):
    return re.sub(r"\{#(\w+)\}", lambda m: defines[m.group(1)], value)


def _section(text, name):
    """The non-comment, non-blank lines of one [Section]."""
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


def _entries(text, section):
    """[Tasks]/[Icons]/[InstallDelete]-style lines as dicts of their parameters."""
    out = []
    for line in _section(text, section):
        params = {}
        for m in re.finditer(r'(\w+):\s*("(?:[^"]|"")*"|[^;]*)', line):
            value = m.group(2).strip()
            if value.startswith('"') and value.endswith('"'):
                value = value[1:-1].replace('""', '"')
            params[m.group(1)] = value
        out.append(params)
    return out


def _code_lines(text):
    """Every line that isn't a comment (';' anywhere, '//' in [Code])."""
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line or line.startswith(";") or line.startswith("//"):
            continue
        out.append(line)
    return out


# ── build.yml / build.ps1 helpers (no YAML parser in the CI test job) ───────

def _jobs(text):
    jobs, name, buf = {}, None, []
    in_jobs = False
    for line in text.splitlines():
        if line.startswith("jobs:"):
            in_jobs = True
            continue
        if not in_jobs:
            continue
        m = re.match(r"^  ([A-Za-z0-9_-]+):\s*$", line)
        if m:
            if name:
                jobs[name] = "\n".join(buf)
            name, buf = m.group(1), []
        elif name:
            buf.append(line)
    if name:
        jobs[name] = "\n".join(buf)
    return jobs


def _steps(job_text):
    """[(name, text)] of a job's steps, in order."""
    steps, cur = [], None
    for line in job_text.splitlines():
        if re.match(r"^      - ", line):
            if cur:
                steps.append(cur)
            m = re.match(r"^      - name:\s*(.+?)\s*$", line)
            cur = [m.group(1) if m else line.strip(), [line]]
        elif cur:
            cur[1].append(line)
    if cur:
        steps.append(cur)
    return [(n, "\n".join(body)) for n, body in steps]


def _step(job_text, name):
    for n, body in _steps(job_text):
        if n == name:
            return body
    raise AssertionError("no step %r" % name)


def _pyinstaller_flags(command):
    """{(flag, value)} of a PyInstaller command line (backticks, '\\' and quotes dropped)."""
    command = command.replace("`", " ").replace("\\\n", " ")
    tokens = re.findall(r'"[^"]*"|\S+', command)
    tokens = [t.strip('"') for t in tokens]
    with_value = {"--name", "--icon", "--version-file", "--add-data", "--hidden-import",
                  "--collect-all", "--exclude-module"}
    flags, i = set(), 0
    while i < len(tokens):
        t = tokens[i]
        if t in with_value and i + 1 < len(tokens):
            flags.add((t, tokens[i + 1]))
            i += 2
            continue
        if t.startswith("--"):
            flags.add((t, None))
        i += 1
    return flags


def _pyinstaller_command(text):
    # The command line itself: "pyinstaller `" (CI) or "& $py -m PyInstaller `" (build.ps1).
    m = re.search(r"^[ \t]*(?:pyinstaller|&\s*\$py\s+-m\s+PyInstaller)[ \t]+`.*?\bmain\.py\b", text, re.S | re.M)
    if not m:
        raise AssertionError("no PyInstaller command")
    return m.group(0)


def _version_info_script(text):
    """The Python between '# version-info:begin' and '# version-info:end'."""
    m = re.search(r"^([ \t]*)# version-info:begin\n(.*?)^[ \t]*# version-info:end", text, re.S | re.M)
    if not m:
        raise AssertionError("no version-info block")
    block = textwrap.dedent(m.group(2))
    body = re.search(r"@'\n(.*?)\n'@", block, re.S)
    if not body:
        raise AssertionError("no @'...'@ here-string in the version-info block")
    return body.group(1) + "\n"


# Stand-ins with the signatures of PyInstaller.utils.win32.versioninfo, which
# eval()s the version file with these names.
def _vs(ffi=None, kids=None):
    return {"ffi": ffi, "kids": kids or []}


def _ffi(filevers=(0, 0, 0, 0), prodvers=(0, 0, 0, 0), mask=0x3F, flags=0x0, OS=0x40004,
         fileType=0x1, subtype=0x0, date=(0, 0)):
    return {"filevers": filevers, "prodvers": prodvers, "fileType": fileType}


def _sfi(kids=None):
    return {"strings": [s for table in (kids or []) for s in table["kids"]]}


def _stable(name=None, kids=None):
    return {"name": name, "kids": kids or []}


def _sstruct(name="", val=""):
    return (name, val)


def _vfi(kids=None):
    return {"var": kids or []}


def _vstruct(name=None, kids=None):
    return (name, kids)


VERSION_INFO_NAMES = {"VSVersionInfo": _vs, "FixedFileInfo": _ffi, "StringFileInfo": _sfi,
                      "StringTable": _stable, "StringStruct": _sstruct, "VarFileInfo": _vfi,
                      "VarStruct": _vstruct}


class TestNameAndVersion(unittest.TestCase):

    def test_app_name_and_version(self):
        import app_info
        self.assertEqual(app_info.APP_NAME, "Gusan")
        self.assertEqual(app_info.APP_VERSION, "2.0.1")

    def test_version_is_plain_three_numbers(self):
        # main._parse_version reads a suffix such as "2.0.0-beta.1" as (0, 0, 0).
        import app_info
        self.assertRegex(app_info.APP_VERSION, r"^\d+\.\d+\.\d+$")

    def test_release_notes_for_this_version(self):
        import app_info
        path = ROOT / "docs" / "release-notes" / ("v%s.md" % app_info.APP_VERSION)
        self.assertTrue(path.is_file(), "write %s" % path.relative_to(ROOT))
        for notes in (path, ROOT / "docs" / "release-notes" / "v2.0.0.md"):
            text = _read(notes)
            self.assertIn("Download file names still start with TranscribeApp, so updates keep working.", text)
            for word in ("limit", "Armenia"):
                self.assertNotIn(word.lower(), text.lower())
        first = next(line for line in _read(ROOT / "docs" / "release-notes" / "v2.0.0.md").splitlines() if line.strip())
        self.assertIn("Transcribe is now Gusan", first)

    def test_release_job_uses_the_notes_file(self):
        release = _jobs(_read(BUILD_YML))["release"]
        self.assertIn('docs/release-notes/${tag}.md', release)


class TestKeptIdentifiers(unittest.TestCase):
    """What existing installs depend on keeps its 1.x name."""

    def test_data_folder_and_keyring(self):
        import storage
        self.assertEqual(storage.APP_DIR_NAME, "Transcribe")
        self.assertEqual(storage.SECRET_SERVICE, "TranscribeApp")

    def test_run_value_name(self):
        import autostart
        self.assertEqual(autostart.VALUE_NAME, "Transcribe")

    def test_updater_asset_and_repo(self):
        import updater
        self.assertEqual(updater.INSTALLER_NAME, INSTALLER_ASSET)
        self.assertEqual(updater.RELEASES_API, "https://api.github.com/repos/Aram2K/Gusan/releases/latest")
        self.assertIn(INSTALLER_ASSET, _read(MAIN_PY))

    def test_single_instance_port_and_handshake(self):
        src = _read(MAIN_PY)
        tree = ast.parse(src)
        port = [n.value.value for n in tree.body
                if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
                and any(isinstance(t, ast.Name) and t.id == "SINGLE_INSTANCE_PORT" for t in n.targets)]
        self.assertEqual(port, [47823])
        handshakes = [n for n in ast.walk(tree) if isinstance(n, ast.Constant) and n.value == b"transcribe-ok"]
        self.assertGreaterEqual(len(handshakes), 2)       # sent by the listener, checked by the caller

    def test_installer_app_id_unchanged(self):
        setup = _setup(_read(ISS))
        self.assertEqual(setup["AppId"], V1_APP_ID)
        v1 = _v1_installer()
        if v1 is None:
            self.skipTest("the 1.x installer isn't in this checkout's history")
        self.assertEqual(setup["AppId"], _setup(v1)["AppId"])

    def test_installer_exe_tasks_and_output_name(self):
        text = _read(ISS)
        d = _defines(text)
        self.assertEqual(d["MyAppExeName"], "TranscribeApp.exe")
        self.assertEqual(_setup(text)["OutputBaseFilename"], "TranscribeApp-Setup")
        self.assertEqual([e["Name"] for e in _entries(text, "Tasks")], ["desktopicon", "startup"])
        v1 = _v1_installer()
        if v1 is not None:
            self.assertEqual(_defines(v1)["MyAppExeName"], d["MyAppExeName"])
            self.assertEqual([e["Name"] for e in _entries(v1, "Tasks")], ["desktopicon", "startup"])

    def test_installer_data_paths_match_storage(self):
        import storage
        found = re.findall(r"\{(userappdata|localappdata)\}\\([^\\'\"\s]+)", _read(ISS))
        self.assertTrue(found)
        for root, folder in found:
            self.assertEqual(folder, storage.APP_DIR_NAME, "{%s}\\%s" % (root, folder))

    def test_build_keeps_every_download_name(self):
        text = _read(BUILD_YML)
        jobs = _jobs(text)
        self.assertIn("--name TranscribeApp", jobs["build-windows"])
        self.assertIn("--name TranscribeApp", jobs["build-macos"])
        self.assertIn("Move-Item installer\\TranscribeApp-Setup.exe %s" % INSTALLER_ASSET, jobs["build-windows"])
        self.assertIn("dist/TranscribeApp.app", jobs["build-macos"])
        for asset in (INSTALLER_ASSET, "TranscribeApp-Windows.zip", "TranscribeApp-Mac.dmg"):
            self.assertIn('"name": "%s"' % asset, jobs["release"])
        files = _step(jobs["release"], "Create GitHub Release")
        for path in ("windows-installer/%s" % INSTALLER_ASSET, "windows-installer/%s.sha256" % INSTALLER_ASSET,
                     "windows-zip/TranscribeApp-Windows.zip", "macos-dmg/TranscribeApp-Mac.dmg",
                     "update-manifest.json"):
            self.assertIn(path, files)


class TestInstaller(unittest.TestCase):

    def setUp(self):
        self.text = _read(ISS)
        self.defines = _defines(self.text)

    def test_people_see_gusan(self):
        import app_info
        self.assertEqual(self.defines["MyAppName"], app_info.APP_NAME)
        setup = _setup(self.text)
        self.assertEqual(setup["AppName"], "{#MyAppName}")
        self.assertEqual(setup["DefaultGroupName"], "{#MyAppName}")
        self.assertIn("Help improve {#MyAppName} by sharing", self.text)

    def test_no_old_name_people_can_see(self):
        allowed_defines = ('#define MyRunValueName "Transcribe"', '#define MyOldAppName "Transcribe"')
        for line in _code_lines(self.text):
            if line in allowed_defines:
                continue
            visible = line.replace("TranscribeApp", "")
            visible = re.sub(r"\\Transcribe(?=[\\'\"])", "", visible)    # the data folders
            self.assertNotIn("Transcribe", visible, line)

    def test_run_value_keeps_its_1x_name(self):
        import autostart
        self.assertEqual(self.defines["MyRunValueName"], autostart.VALUE_NAME)
        run_lines = [line for line in _code_lines(self.text) if "CurrentVersion\\Run" in line
                     or "StartupApproved\\Run" in line]
        named = [line for line in run_lines
                 if "ValueName" in line or "RegDeleteValue" in line or "RegQueryStringValue" in line]
        # [Registry] write, the fresh-install StartupApproved clean-up, and the
        # uninstall query plus its two deletes.
        self.assertGreaterEqual(len(named), 5)
        for line in run_lines:
            self.assertNotIn("{#MyAppName}", line)
        for line in named:
            self.assertIn("{#MyRunValueName}", line)

    def test_new_installs_go_to_gusan_upgrades_keep_their_folder(self):
        setup = _setup(self.text)
        self.assertEqual(setup["DefaultDirName"], "{autopf}\\{#MyAppName}")
        self.assertNotEqual(setup.get("UsePreviousAppDir", "yes").lower(), "no")
        self.assertEqual(setup["UsePreviousGroup"].lower(), "no")

    def test_install_delete_removes_the_old_shortcuts(self):
        names = {(e["Type"], _expand(e["Name"], self.defines)) for e in _entries(self.text, "InstallDelete")}
        old = V1_APP_NAME
        for entry in (("files", "{autoprograms}\\%s\\%s.lnk" % (old, old)),
                      ("files", "{autoprograms}\\%s\\Uninstall %s.lnk" % (old, old)),
                      ("dirifempty", "{autoprograms}\\%s" % old),
                      ("files", "{autodesktop}\\%s.lnk" % old)):
            self.assertIn(entry, names)

    def test_install_delete_covers_every_1x_shortcut(self):
        v1 = _v1_installer()
        if v1 is None:
            self.skipTest("the 1.x installer isn't in this checkout's history")
        d1 = _defines(v1)
        group = "{autoprograms}\\" + _expand(_setup(v1)["DefaultGroupName"], d1)
        names = {_expand(e["Name"], self.defines) for e in _entries(self.text, "InstallDelete")}
        for icon in _entries(v1, "Icons"):
            path = _expand(icon["Name"], d1).replace("{group}", group) + ".lnk"
            self.assertIn(path, names)

    def test_install_delete_never_touches_data_or_the_migrated_startup_shortcut(self):
        # The app moves an old {userstartup}\Transcribe.lnk to the Run value on its
        # first launch, keeping Task Manager's switch (autostart.LEGACY_SHORTCUT).
        for e in _entries(self.text, "InstallDelete"):
            name = _expand(e["Name"], self.defines)
            for root in ("{userstartup}", "{commonstartup}", "{app}", "{userappdata}", "{localappdata}"):
                self.assertFalse(name.startswith(root), name)

    def test_shortcuts_are_named_gusan(self):
        icons = [_expand(e["Name"], self.defines) for e in _entries(self.text, "Icons")]
        self.assertEqual(icons, ["{group}\\Gusan", "{group}\\Uninstall Gusan", "{autodesktop}\\Gusan"])


class TestBuild(unittest.TestCase):

    def setUp(self):
        self.yml = _read(BUILD_YML)
        self.jobs = _jobs(self.yml)
        self.ps1 = _read(BUILD_PS1)

    def test_version_check_reads_app_info_and_matches_the_tag(self):
        import app_info
        src = _read(APP_INFO_PY)
        win = _step(self.jobs["build-windows"], "Check the version")
        mac = _step(self.jobs["build-macos"], "Check the version")
        for step in (win, mac, self.ps1):
            self.assertIn("app_info.py", step)
            self.assertIn("does not match APP_VERSION", step)
            self.assertNotRegex(step, r"(?m)^[^#\n]*\b(?:import main|from main import)\b")
        # The PowerShell pattern (build.yml and build.ps1) finds APP_VERSION.
        for text in (win, self.ps1):
            pattern = re.search(r"-notmatch '([^']+)'", text).group(1)
            self.assertEqual(re.search(pattern, src).group(1), app_info.APP_VERSION)
        # The Mac job's sed pattern, translated to Python.
        sed = re.search(r"sed -nE 's/(.+?)/\\1/p' app_info\.py", mac).group(1)
        sed = sed.replace("[[:space:]]", r"\s")
        hits = [re.match(sed, line).group(1) for line in src.splitlines() if re.match(sed, line)]
        self.assertEqual(hits[:1], [app_info.APP_VERSION])
        # Every later Windows step uses the checked version.
        steps = [n for n, _ in _steps(self.jobs["build-windows"])]
        self.assertLess(steps.index("Check the version"), steps.index("Build .exe"))
        self.assertIn("${{ steps.version.outputs.version }}", _step(self.jobs["build-windows"],
                                                                    "Build Inno Setup installer"))

    def test_version_info_script_is_the_same_in_ci_and_build_ps1(self):
        self.assertEqual(_version_info_script(self.yml), _version_info_script(self.ps1))

    def test_version_info_says_gusan_by_aibuben(self):
        import app_info
        script = _version_info_script(self.yml)
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "version_info.txt")
            run = subprocess.run([sys.executable, "-", out], input=script, cwd=str(ROOT),
                                 capture_output=True, text=True, timeout=60)
            self.assertEqual(run.returncode, 0, run.stderr)
            text = Path(out).read_text(encoding="utf-8")
        info = eval(text, {"__builtins__": {}}, dict(VERSION_INFO_NAMES))   # as PyInstaller loads it
        strings = dict(info["kids"][0]["strings"])
        dotted = app_info.APP_VERSION + ".0"
        self.assertEqual(strings["ProductName"], "Gusan")
        self.assertEqual(strings["FileDescription"], "Gusan")
        self.assertEqual(strings["CompanyName"], "AIBUBEN")
        self.assertEqual(strings["OriginalFilename"], "TranscribeApp.exe")
        self.assertEqual(strings["FileVersion"], dotted)
        self.assertEqual(strings["ProductVersion"], dotted)
        self.assertEqual(dotted, "2.0.1.0")
        nums = tuple(int(x) for x in dotted.split("."))
        self.assertEqual(info["ffi"]["filevers"], nums)
        self.assertEqual(info["ffi"]["prodvers"], nums)
        try:
            from PyInstaller.utils.win32 import versioninfo
        except Exception:
            return                                         # not installed here: the stand-ins did the check
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "v.txt")
            Path(path).write_text(text, encoding="utf-8")
            self.assertIn("Gusan", str(versioninfo.load_version_info_from_text_file(path)))

    def test_exe_gets_the_version_file(self):
        steps = [n for n, _ in _steps(self.jobs["build-windows"])]
        self.assertLess(steps.index("Write the Windows version info"), steps.index("Build .exe"))
        self.assertIn("python - build/version_info.txt", _step(self.jobs["build-windows"],
                                                               "Write the Windows version info"))
        flags = _pyinstaller_flags(_pyinstaller_command(_step(self.jobs["build-windows"], "Build .exe")))
        self.assertIn(("--version-file", "build/version_info.txt"), flags)
        self.assertIn(("--name", "TranscribeApp"), flags)

    def test_build_ps1_builds_what_ci_builds(self):
        ci = _pyinstaller_flags(_pyinstaller_command(_step(self.jobs["build-windows"], "Build .exe")))
        local = _pyinstaller_flags(_pyinstaller_command(self.ps1))
        self.assertEqual(ci - {("--noconfirm", None)}, local - {("--noconfirm", None)})
        self.assertIn("installer/make_icon.py", self.ps1)
        self.assertNotRegex(self.ps1, r"(?m)^[^#\n]*Activate\.ps1")    # the venv's python.exe -m, not pip.exe

    def test_pyinstaller_is_pinned_to_one_version(self):
        pins = set()
        for text in (self.yml, self.ps1):
            for line in text.splitlines():
                if "pip install" in line and re.search(r"(?i)\bpyinstaller\b", line):
                    m = re.search(r"(?i)\bpyinstaller==(\d+\.\d+(?:\.\d+)?)\b", line)
                    self.assertIsNotNone(m, "unpinned PyInstaller: %s" % line.strip())
                    pins.add(m.group(1))
        self.assertEqual(len(pins), 1, pins)

    def test_mac_bundle_says_gusan(self):
        mac = self.jobs["build-macos"]
        plist = _step(mac, "Patch Info.plist (name, version, usage strings)")
        self.assertIn('plist_set CFBundleName "Gusan"', plist)
        self.assertIn('plist_set CFBundleDisplayName "Gusan"', plist)
        self.assertIn('plist_set CFBundleShortVersionString "$VERSION"', plist)
        self.assertIn('plist_set CFBundleVersion "$VERSION"', plist)
        self.assertIn('VERSION="${{ steps.version.outputs.app_version }}"', plist)
        usage = re.findall(r'plist_set NS\w+UsageDescription "([^"]+)"', plist)
        self.assertEqual(len(usage), 3)
        for text in usage:
            self.assertTrue(text.startswith("Gusan "), text)
        self.assertNotRegex(plist, r"['\"]Transcribe ")
        self.assertIn('-volname "Gusan"', _step(mac, "Package as DMG"))
        self.assertNotIn("--osx-bundle-identifier", mac)   # the bundle id stays what --name gives

    def test_qt_window_tests_run_in_the_windows_job(self):
        win = self.jobs["build-windows"]
        step = _step(win, "Qt window tests (offscreen)")
        self.assertIn("QT_QPA_PLATFORM: offscreen", step)
        self.assertRegex(step, r"pytest -q\b[^\n]*\btests_qt\b")
        self.assertIn("pytest-xdist", step)                  # the suite is too long for one process
        self.assertIn("--dist loadfile", step)               # a test file stays in one process
        self.assertIn("--maxprocesses 3", step)              # one core stays free for the timing tests
        # The window-freeze timing tests run alone, after the rest (and never twice).
        self.assertIn("--ignore=tests_qt/test_gui_stall_budget.py", step)
        self.assertRegex(step, r"pytest -q tests_qt/test_gui_stall_budget\.py")
        self.assertRegex(step, r"(?m)^\s+timeout-minutes:\s*\d+\s*$")    # a hung test can't hold the job
        steps = [n for n, _ in _steps(win)]
        self.assertLess(steps.index("Install dependencies"), steps.index("Qt window tests (offscreen)"))
        self.assertLess(steps.index("Qt window tests (offscreen)"), steps.index("Build .exe"))


# Repo modules the app no longer imports, so PyInstaller leaves them out on
# purpose: the retired 1.x windows (tests/test_routes_surfaces.py
# TestRetiredWindows) and meeting_share (helpers kept for a future Share action).
# (Built from bare names: TestRetiredWindows flags any quoted "ui.<retired>".)
_RETIRED_UI = ("settings", "history", "meeting_detail", "meetings", "onboarding", "shiny_tabs",
               "mode_combo", "file_transcribe")
NOT_BUNDLED = {"ui." + name for name in _RETIRED_UI} | {"meeting_share"}
_NOT_APP_DIRS = {"tests", "tests_qt", "installer", "docs", "venv", "build", "dist", "assets",
                 "supabase", "node_modules", "__pycache__"}


def _app_modules():
    mods = {}
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in _NOT_APP_DIRS and not d.startswith(".")]
        for fn in filenames:
            if fn.endswith(".py"):
                path = os.path.join(dirpath, fn)
                name = os.path.relpath(path, ROOT)[:-3].replace(os.sep, ".")
                if name.endswith(".__init__"):
                    name = name[: -len(".__init__")]
                mods[name] = path
    return mods


def _imports(name, path, mods):
    """Repo modules ``name`` imports with import statements (inside functions too,
    which is what PyInstaller's analysis follows), plus every imported name."""
    tree = ast.parse(_read(path), path)
    pkg = name if path.endswith("__init__.py") else name.rpartition(".")[0]
    names = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                parts = pkg.split(".") if pkg else []
                parts = parts[: len(parts) - (node.level - 1)]
                base = ".".join(parts + ([base] if base else []))
            names.add(base)
            names.update(base + "." + a.name if base else a.name for a in node.names)
    found = set()
    for full in names:
        parts = full.split(".")
        found.update(".".join(parts[:i]) for i in range(1, len(parts) + 1) if ".".join(parts[:i]) in mods)
    return found, names


class TestBundling(unittest.TestCase):
    """PyInstaller bundles what main.py reaches through import statements."""

    @classmethod
    def setUpClass(cls):
        cls.mods = _app_modules()
        cls.reached, cls.names, todo = set(), set(), ["main"]
        while todo:
            m = todo.pop()
            if m in cls.reached:
                continue
            cls.reached.add(m)
            found, names = _imports(m, cls.mods[m], cls.mods)
            cls.names |= names
            todo.extend(found - cls.reached)

    def test_every_app_module_is_reached_by_plain_imports(self):
        missing = sorted(set(self.mods) - self.reached - NOT_BUNDLED)
        self.assertEqual(missing, [], "import these with plain import statements from the app "
                                      "(or add --hidden-import in build.yml and build.ps1)")

    def test_the_new_window_and_services_are_reached(self):
        pages = {m for m in self.mods if m.startswith(("ui.pages.", "ui.components.", "ui.floating."))}
        self.assertTrue(pages)
        for m in sorted(pages | {"ui.pages", "ui.components", "ui.floating", "ui.shell", "ui.theme",
                                 "ui.tray_menu", "ui.onboarding_v2", "ui.auth_sheet", "app_info",
                                 "settings_apply", "updater", "file_jobs", "model_manager",
                                 "meeting_session", "calendar_service", "mic_monitor"}):
            self.assertIn(m, self.reached, m)

    def test_qt_svg_is_imported_plainly(self):
        self.assertIn("PySide6.QtSvg", self.names)

    def test_qt_multimedia_is_imported_plainly(self):
        # The meeting player (ui/components/audio_player.py) imports it inside a
        # method; PyInstaller still sees it and adds Qt's multimedia plugins.
        # The Windows build (CI and build.ps1, the same lines) fails without them.
        self.assertIn("PySide6.QtMultimedia", self.names)
        pattern = r"^([ \t]*)# multimedia-check:begin\n(.*?)^[ \t]*# multimedia-check:end"
        win = _step(_jobs(_read(BUILD_YML))["build-windows"], "Build .exe")
        blocks = [textwrap.dedent(re.search(pattern, text, re.S | re.M).group(2))
                  for text in (win, _read(BUILD_PS1))]
        self.assertEqual(blocks[0], blocks[1])
        self.assertIn(r"_internal\PySide6\plugins\multimedia\windowsmediaplugin.dll", blocks[0])


class TestDocs(unittest.TestCase):

    def test_readme_says_gusan_and_keeps_the_links(self):
        text = _read(ROOT / "README.md")
        self.assertIn("Gusan was called Transcribe until", text)
        for asset in (INSTALLER_ASSET, "TranscribeApp-Mac.dmg", "TranscribeApp-Windows.zip"):
            self.assertIn("https://github.com/Aram2K/Gusan/releases/latest/download/" + asset, text)
        self.assertIn("%APPDATA%\\Transcribe", text)
        for old in ("Transcribe App", "Transcribe Pro", "What is Transcribe", "Live Assistance"):
            self.assertNotIn(old, text)
        # The 1.x windows are gone: no steps that point at them.
        for retired in ("Settings → History", "Settings → Actions", "Stop & generate notes", "Resume session",
                        "Settings › Updates ›", "eye badge"):
            self.assertNotIn(retired, text)

    def test_releasing_notes_the_version_checks(self):
        text = _read(ROOT / "RELEASING.md")
        self.assertIn("# Releasing Gusan", text)
        self.assertIn("app_info.py", text)
        self.assertIn("fails when a tag differs", text)
        self.assertNotIn("APP_VERSION` in `main.py", text)
        self.assertIn(INSTALLER_ASSET, text)


if __name__ == "__main__":
    unittest.main()
