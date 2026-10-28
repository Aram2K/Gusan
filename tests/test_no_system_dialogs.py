"""No live code asks the OS for a message box again.

A QMessageBox / QInputDialog / QProgressDialog / QErrorMessage is the OS's box: its panel follows the
OS colour scheme while the app's stylesheet gives its words one colour of their own - on a Windows in
dark mode that was navy text on dark grey (the founder's "Delete meeting" box, 2026-10; see ui/theme.py
"One light look"). Every question and message is ui/components/mini_prompt.py (confirm, notify, choose,
ask_text, ask) instead, which paints its own card. This file reads the source (the syntax tree, so a
docstring or a stylesheet that merely MENTIONS a class does not count) - it needs no Qt, so it runs on
every CI - and fails on:

  * any use of those four classes in main.py, the top-level modules, ui/ or the live pages;
  * a bare QDialog(...) made from code (a subclass with its own look is fine): the macOS permissions
    guide is the one allowed, and it styles itself fully.

The 1.x windows that Gusan 2.0 retired (ui/settings.py ... ) still hold such calls, but nothing imports
them (tests/test_routes_surfaces.py TestRetiredWindows) and they wait for the founder's OK to be deleted:
they are listed here, so they stay visible, and a NEW file is never exempt.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
BANNED = {"QMessageBox", "QInputDialog", "QProgressDialog", "QErrorMessage",
          # the OS's own boxes by another door: Win32's MessageBox (ctypes) and Tk's
          "MessageBoxW", "MessageBoxA", "MessageBoxExW", "MessageBoxExA", "tkinter", "messagebox", "simpledialog"}
# Retired 1.x windows: never imported (TestRetiredWindows guards that), waiting to be deleted.
RETIRED = {os.path.join("ui", n) for n in ("settings.py", "history.py", "meeting_detail.py", "meetings.py",
                                           "onboarding.py", "shiny_tabs.py", "mode_combo.py", "file_transcribe.py")}
# (file, function) that may build a bare QDialog.
BARE_DIALOG_OK = {("main.py", "show_macos_permissions_guide")}
SKIP_DIRS = {"venv", ".venv", ".git", ".claude", "build", "dist", "__pycache__", "tests", "tests_qt", "docs",
             "installer", "assets", "supabase", "node_modules"}


def live_sources():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(base, name)
            rel = os.path.relpath(path, ROOT)
            if rel in RETIRED or rel.startswith("scratch"):
                continue
            yield rel, path


def _tree(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return ast.parse(f.read(), filename=path)


def banned_uses(tree):
    """[(line, name)] where code imports or names one of the banned classes."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            out += [(node.lineno, a.name) for a in node.names if a.name in BANNED]
            out += [(node.lineno, p) for p in (node.module or "").split(".") if p in BANNED]
        elif isinstance(node, ast.Import):
            out += [(node.lineno, p) for a in node.names for p in a.name.split(".") if p in BANNED]
        elif isinstance(node, ast.Name) and node.id in BANNED:
            out.append((node.lineno, node.id))
        elif isinstance(node, ast.Attribute) and node.attr in BANNED:
            out.append((node.lineno, node.attr))
    return out


def bare_dialogs(tree):
    """[(line, enclosing function)] of every ``QDialog(...)`` call."""
    found = []

    def visit(node, func):
        for child in ast.iter_child_nodes(node):
            f = child.name if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) else func
            if (isinstance(child, ast.Call) and isinstance(child.func, ast.Name) and child.func.id == "QDialog") or (
                    isinstance(child, ast.Call) and isinstance(child.func, ast.Attribute) and child.func.attr == "QDialog"):
                found.append((child.lineno, func))
            visit(child, f)
    visit(tree, "")
    return found


class TestNoSystemDialogs(unittest.TestCase):
    def test_the_detector_sees_real_uses_and_not_words(self):
        src = ('"""QMessageBox is mentioned in this docstring."""\n'
               'SHEET = "QMessageBox QPushButton { color: red; }"\n'
               'from PySide6.QtWidgets import QMessageBox, QLabel\n'
               'def f(parent):\n'
               '    QMessageBox.question(parent, "t", "x")\n'
               '    box = QtWidgets.QInputDialog(parent)\n'
               '    d = QDialog()\n')
        tree = ast.parse(src)
        self.assertEqual(sorted(banned_uses(tree)), [(3, "QMessageBox"), (5, "QMessageBox"), (6, "QInputDialog")])
        self.assertEqual(bare_dialogs(tree), [(7, "f")])
        self.assertEqual(banned_uses(ast.parse('"""QMessageBox"""\nX = "QInputDialog"\n')), [])

    def test_the_detector_sees_the_other_ways_to_open_an_os_box(self):
        src = ('import ctypes\n'
               'import tkinter.messagebox\n'
               'from tkinter import messagebox\n'
               'def f():\n'
               '    ctypes.windll.user32.MessageBoxW(0, "x", "y", 0)\n')
        names = sorted(name for _line, name in banned_uses(ast.parse(src)))
        self.assertEqual(names, ["MessageBoxW", "messagebox", "messagebox", "tkinter", "tkinter"])

    def test_no_live_module_opens_a_system_message_box(self):
        found = []
        for rel, path in live_sources():
            found += ["%s:%d %s" % (rel, line, name) for line, name in banned_uses(_tree(path))]
        self.assertEqual(found, [], "use ui.components.mini_prompt (confirm / notify / choose / ask_text) instead")

    def test_no_bare_qdialog_is_made_from_code(self):
        found = []
        for rel, path in live_sources():
            for line, func in bare_dialogs(_tree(path)):
                if (rel.replace(os.sep, "/"), func) not in BARE_DIALOG_OK:
                    found.append("%s:%d in %s()" % (rel, line, func or "<module>"))
        self.assertEqual(found, [], "a question is a MiniPrompt; a window is a subclass with its own look")

    def test_the_exempt_windows_are_really_unused(self):
        """The exemption is only for 1.x windows nobody imports (so a live module can't hide in it)."""
        users = []
        for rel, path in live_sources():
            with open(path, encoding="utf-8", errors="ignore") as f:
                text = f.read()
            # (spelled in two parts: tests/test_routes_surfaces.py looks for these names in quotes)
            for mod in ["ui." + n for n in ("settings", "history", "meeting_detail", "meetings", "onboarding",
                                            "shiny_tabs", "mode_combo", "file_transcribe")]:
                for line in text.splitlines():
                    s = line.strip()
                    if s in ("import " + mod, ) or s.startswith(("from %s import" % mod, "import %s " % mod)):
                        users.append("%s: %s" % (rel, s))
        self.assertEqual(users, [])


if __name__ == "__main__":
    unittest.main()
