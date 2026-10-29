"""Every Connect button reaches Google Calendar through ONE door: ui/calendar_door.connect_calendar.

Google requires the app to say what it reads and why BEFORE the sign-in opens, so the disclosure box
(mini_prompt.ask in that function) comes first and "Not now" does nothing. The Meetings page, the Home page's
"Coming up" invitation and any later Connect button (Settings, the tray...) must all use it. Each button's
behaviour is tested where the button is (tests_qt/test_calendar_ui.py, test_home_upcoming.py); this file reads
the source - the syntax tree, so a docstring that only MENTIONS the name does not count; it needs no Qt, so
it runs on every CI - and fails when something else can start the sign-in:

  * any ``X.connect_calendar`` (called, or handed over as a slot: ``clicked.connect(cal.connect_calendar)``)
    outside ui/calendar_door.py whose X is not one of the known doors below;
  * ``getattr(X, "connect_calendar")``.

The service's own definition of the method (calendar_service.py) is a ``def``, not a use.
"""
import ast
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NAME = "connect_calendar"
DOOR = "ui/calendar_door.py"
# (file, what is on the left of the dot) that may use the name: the door's callers. A new entry is a decision
# somebody has to defend in review: "this Connect button goes through the disclosure box".
ALLOWED = {
    ("ui/calendar_door.py", "cal"): "the door itself: asks, then starts the sign-in on the service",
    ("ui/pages/home_upcoming.py", "calendar_door"): "Home's invitation card: through the door",
    ("ui/pages/meetings.py", "calendar_door"): "MeetingsPage.connect_calendar: through the door",
    ("ui/pages/meetings.py", "self.page"): "the Meetings page's Connect buttons: MeetingsPage.connect_calendar",
}
SKIP_DIRS = {"venv", ".venv", ".git", ".claude", "build", "dist", "__pycache__", "tests", "tests_qt", "docs",
             "installer", "assets", "supabase", "node_modules"}


def live_sources():
    for base, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if name.endswith(".py"):
                path = os.path.join(base, name)
                rel = os.path.relpath(path, ROOT).replace(os.sep, "/")
                if not rel.startswith("scratch"):
                    yield rel, path


def _tree(path):
    with open(path, encoding="utf-8", errors="ignore") as f:
        return ast.parse(f.read(), filename=path)


def uses(tree, rel):
    """[(line, what)] where the name is reached by a receiver that is not a known door."""
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Attribute) and node.attr == NAME:
            receiver = ast.unparse(node.value)
            if (rel, receiver) not in ALLOWED:
                out.append((node.lineno, "%s.%s" % (receiver, NAME)))
        elif (isinstance(node, ast.Call) and isinstance(node.func, ast.Name) and node.func.id == "getattr"
              and len(node.args) >= 2 and isinstance(node.args[1], ast.Constant) and node.args[1].value == NAME):
            out.append((node.lineno, "getattr(..., %r)" % NAME))
    return out


class TestOneConnectDoor(unittest.TestCase):
    def test_the_detector_sees_real_uses_and_not_words(self):
        src = ('"""cal.connect_calendar() is mentioned in this docstring."""\n'
               'NOTE = "x.connect_calendar()"\n'
               'class Service:\n'
               '    def connect_calendar(self):\n'
               '        return True\n'
               'def a(cal):\n'
               '    cal.connect_calendar()\n'
               'def b(self, button):\n'
               '    button.clicked.connect(self.app.calendar.connect_calendar)\n'
               'def c(cal):\n'
               '    return getattr(cal, "connect_calendar")()\n'
               'def d(self):\n'
               '    self.page.connect_calendar()\n')
        tree = ast.parse(src)
        self.assertEqual(sorted(uses(tree, "ui/pages/settings.py")),
                         [(7, "cal.connect_calendar"), (9, "self.app.calendar.connect_calendar"),
                          (11, "getattr(..., 'connect_calendar')"), (13, "self.page.connect_calendar")])
        self.assertEqual([w for _line, w in uses(tree, "ui/pages/meetings.py")],
                         ["cal.connect_calendar", "self.app.calendar.connect_calendar",
                          "getattr(..., 'connect_calendar')"])                 # (self.page is the Meetings page's own)
        self.assertEqual(uses(ast.parse('"""connect_calendar"""\nX = "a.connect_calendar"\n'), "x.py"), [])

    def test_only_the_door_and_its_known_callers_use_the_name(self):
        found = []
        for rel, path in live_sources():
            with open(path, encoding="utf-8", errors="ignore") as f:
                if NAME not in f.read():
                    continue
            found += ["%s:%d %s" % (rel, line, what) for line, what in uses(_tree(path), rel)]
        self.assertEqual(found, [], "a Connect button goes through ui/calendar_door.connect_calendar "
                                    "(the disclosure box first), not straight to the service")

    def test_the_allowed_callers_still_exist_and_use_the_name(self):
        """No stale entry: each (file, receiver) in ALLOWED is really used, so a removed caller is removed here."""
        for (rel, receiver), why in ALLOWED.items():
            tree = _tree(os.path.join(ROOT, *rel.split("/")))
            seen = [n for n in ast.walk(tree)
                    if isinstance(n, ast.Attribute) and n.attr == NAME and ast.unparse(n.value) == receiver]
            self.assertTrue(seen, "%s no longer uses %s.%s (%s): take it out of ALLOWED" % (rel, receiver, NAME, why))

    def test_the_door_asks_before_it_starts_the_sign_in(self):
        tree = _tree(os.path.join(ROOT, *DOOR.split("/")))
        fn = next(n for n in tree.body if isinstance(n, ast.FunctionDef) and n.name == NAME)
        asks = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                and n.func.attr == "ask" and ast.unparse(n.func.value) == "mini_prompt"]
        starts = [n.lineno for n in ast.walk(fn) if isinstance(n, ast.Attribute) and n.attr == NAME
                  and ast.unparse(n.value) == "cal"]
        self.assertEqual(len(asks), 1)
        self.assertEqual(len(starts), 1)
        self.assertLess(asks[0], starts[0], "the disclosure box comes before the sign-in")


if __name__ == "__main__":
    unittest.main()
