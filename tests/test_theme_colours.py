"""No colour is written into the UI code: every colour the app paints comes from ui/theme.py's tokens.

Dark mode (2.2) switches the whole app between two token sets at run time, with no restart. A colour
written straight into a page (a hex, an rgb()/rgba(), a QColor(r, g, b), Qt.white / Qt.black, the words
"white" / "black" in a sheet) stays the same in both looks - a white box on the dark window, or dark
words on it. So this test reads the code of every module that paints UI (everything under ui/ and the
root modules listed in ROOT_UI) and fails on any colour literal outside ui/theme.py, except the ones in
ALLOWED below: brand and identity colours that must never change (the Armenian flag in the logo, the
avatar presets, Google's sign-in colours, the Pro purple), each with its reason.

Comments and docstrings are not code: only string literals, QColor(...) calls with numbers and Qt's
named colours are read (Python's own parser, so a colour split over an f-string is still found).
QColor(0, 0, 0, 0) and the like - fully transparent - are not a colour.

When this fails: use a token (theme.qcolor("muted") when painting, theme.sheet(widget, "... @muted ...")
for a widget's own sheet, an icon in a token colour), or - only for a colour that is part of an
identity, never for a surface or for words - add it to ALLOWED with the reason.
"""
import ast
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Root modules that paint UI (the rest of the root is services with no pixels).
ROOT_UI = ("main.py", "splash_window.py", "rich_text.py", "live_rules.py", "logo.py")

HEX = re.compile(r"(?<![\w&/])#(?:[0-9a-fA-F]{8}|[0-9a-fA-F]{6}|[0-9a-fA-F]{3})(?![0-9a-zA-Z_])")
RGB = re.compile(r"rgba?\(\s*\d{1,3}\s*,\s*\d{1,3}\s*,\s*\d{1,3}\s*(?:,\s*[\d.]+\s*)?\)")
NAMED_IN_CSS = re.compile(r"(?:color|background|background-color|border(?:-[a-z]+)?)\s*:\s*[^;\"'}]*?\b(white|black)\b")
NAMED = {"white", "black"}

# path -> {literal: reason}. A literal is lower-case, written as in the code ("#ffffff",
# "rgba(255,255,255,0.59)" with no spaces, "qcolor(15,23,42)", "qt.white", "white").
ALLOWED = {
    "main.py": {
        "#3b82f6": "the tray icon's accent (config accent_color, make_icon/make_qicon): identity, the same in both looks",
    },
    "ui/icons.py": {
        "#a855f7": "PRO pill icon fill: the brand purple behind white words, same in both looks",
        "#16a34a": "LOCAL pill icon fill: a strong green behind white words, same in both looks",
        "#0284c7": "CLOUD pill icon fill: a strong blue behind white words, same in both looks",
    },
    "ui/pages/meeting_views.py": {
        "#60a5fa": "the user's own face in a meeting: the sidebar avatar's blue gradient top (identity)",
    },
    "ui/floating/meeting_reminder.py": {
        "#ff3b4e": "the record dot's red (the mockup's rec_dot), the same in both looks",
    },
    "ui/onboarding_v2.py": {
        "#f472b6": "Anna's avatar gradient in the welcome's mail demo (an avatar preset: identity)",
        "#db2777": "Anna's avatar gradient in the welcome's mail demo (an avatar preset: identity)",
    },
}

# path -> {name: reason}: every colour inside the assignment to ``name`` (a module or class constant)
# or inside the function / class ``name`` is allowed - a palette that defines BOTH looks itself, or an
# identity drawn in its own colours. Narrow on purpose: the block, not the file.
ALLOWED_BLOCKS = {
    "ui/components/avatars.py": {
        "PRESETS": "avatar presets: identity gradients the person picks, never change with the look",
    },
    "ui/pages/dictation.py": {
        "BAR_GLASS": "the dictation pill's own light and dark glass (ui/overlay.py _PALETTES), shown as previews",
        "BACKDROP_LIGHT": "the light backdrop of the pill-look preview (the pill over light content)",
        "BACKDROP_DARK": "the dark backdrop of the pill-look preview (the pill over dark content)",
        "_mini_pill": "the tiny pill in the Auto/Light/Dark look picker, drawn in the pill's own two glasses",
    },
    "ui/live_assist.py": {
        "_THEMES": "the card's own two looks (light and dark glass): every colour it paints, per look",
        "_CHIP_COLORS": "the Hidden chip's colours per card look (deep tints on light glass, bright on dark)",
        "_BRAND": "the card's brand fills (send, Start, the PRO pill): the same in both looks",
        "_OVER_IMAGE": "drawn over a screenshot (attached image, crop view): the same on either look",
        "_JPEG_MATTE": "a picture sent as JPEG: its transparent part becomes white (never on screen)",
    },
    "ui/pages/live_demo.py": {
        "_GLASS": "the card's two looks in miniature, value for value the real card's _THEMES (a test keeps them equal)",
        "_FIXED": "the miniature's brand send disc, stop square, glass light and plain backdrop: the same in both looks",
    },
    "ui/pages/live_assistant.py": {
        "_HERO_QSS": "Open Live Assistant: the brand's blue-to-violet button, the same in both looks",
        "_FACES_YOU": "the call's people in the picture: avatar colours (an identity)",
        "_FACES_THEM": "the call's people in the picture: avatar colours (an identity)",
        "_PICTURE": "the You see / They see picture's own colours per look (monitor, the slide on it, line, handle)",
    },
    "ui/overlay.py": {
        "_PALETTES": "the dictation pill's own two glasses (light and dark), picked by the backdrop or pinned",
        "_WHITE": "the white of a highlight, a tick or a '!' on the pill's coloured badges: the same on both glasses",
    },
    "ui/tray_menu.py": {
        "COLORS": "the system tray menu's colours, light and dark: it follows the OS menu look, not the app's",
        "_PRO_PILL": "the PRO pill's brand purple and white word, the same on a light and a dark menu",
    },
    "ui/auth_sheet.py": {
        "GOOGLE_LOOKS": "Google's own sign-in button colours (its branding guidelines: the light button, and its dark "
                        "one in the dark look)",
    },
}


def _files():
    out = []
    for base, dirs, files in os.walk(os.path.join(ROOT, "ui")):
        dirs[:] = [d for d in dirs if d != "__pycache__"]
        for f in files:
            if f.endswith(".py"):
                out.append(os.path.join(base, f))
    out += [os.path.join(ROOT, f) for f in ROOT_UI if os.path.exists(os.path.join(ROOT, f))]
    return sorted(p for p in out if os.path.relpath(p, ROOT).replace("\\", "/") != "ui/theme.py")


def _docstrings(tree):
    ids = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            body = getattr(node, "body", None) or []
            if body and isinstance(body[0], ast.Expr) and isinstance(getattr(body[0], "value", None), ast.Constant) \
                    and isinstance(body[0].value.value, str):
                ids.add(id(body[0].value))
    return ids


def _num(node):
    if isinstance(node, ast.Constant) and isinstance(node.value, (int, float)) and not isinstance(node.value, bool):
        return node.value
    if isinstance(node, ast.UnaryOp) and isinstance(node.op, ast.USub):
        v = _num(node.operand)
        return -v if v is not None else None
    return None


def _block_lines(tree, names):
    """The line ranges of the assignments to / definitions of ``names``."""
    spans = []
    if not names:
        return spans
    for node in ast.walk(tree):
        hit = None
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)) and node.name in names:
            hit = node.name
        elif isinstance(node, (ast.Assign, ast.AnnAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for t in targets:
                n = t.id if isinstance(t, ast.Name) else getattr(t, "attr", None)
                if n in names:
                    hit = n
        if hit:
            spans.append((hit, node.lineno, node.end_lineno))
    return spans


def literals(source, blocks=()):
    """[(line, literal)] of every colour written in ``source`` (outside the named ``blocks``)."""
    tree = ast.parse(source)
    docs = _docstrings(tree)
    spans = _block_lines(tree, set(blocks))
    found = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str) and id(node) not in docs:
            s = node.value
            for m in HEX.finditer(s):
                found.append((node.lineno, m.group(0).lower()))
            for m in RGB.finditer(s):
                found.append((node.lineno, re.sub(r"\s+", "", m.group(0).lower())))
            for m in NAMED_IN_CSS.finditer(s):
                found.append((node.lineno, m.group(1).lower()))
            if s.strip().lower() in NAMED:
                found.append((node.lineno, s.strip().lower()))
        elif isinstance(node, ast.Call):
            fn = node.func
            name = fn.attr if isinstance(fn, ast.Attribute) else getattr(fn, "id", "")
            if name == "QColor" and len(node.args) >= 3:
                nums = [_num(a) for a in node.args[:4]]
                if all(v is not None for v in nums[:3]):
                    if len(nums) == 4 and nums[3] == 0:
                        continue                                      # fully transparent
                    found.append((node.lineno, "qcolor(%s)" % ",".join(str(v) for v in nums if v is not None)))
        elif isinstance(node, ast.Attribute) and node.attr in ("white", "black") and \
                isinstance(node.value, (ast.Name, ast.Attribute)) and \
                (getattr(node.value, "id", "") == "Qt" or getattr(node.value, "attr", "") == "GlobalColor"):
            found.append((node.lineno, "qt." + node.attr))
    return sorted({(ln, lit) for ln, lit in found if not any(a <= ln <= b for _n, a, b in spans)})


class ColourLiteralsTest(unittest.TestCase):
    def test_the_detector_finds_every_kind(self):
        src = ('"""#123456 in a docstring is fine."""\n'
               'A = "color: #1e293b; background: rgba(15, 23, 42, 0.5)"\n'
               'B = QColor(15, 23, 42)\n'
               'C = QColor(0, 0, 0, 0)\n'
               'D = Qt.white\n'
               'E = f"border: 1px solid {x}; color: white"\n'
               'F = "white"\n'
               '# "#abcdef" in a comment is fine\n'
               'G = "&#123; html entity, url(#frag) and #fff"\n')
        got = [lit for _l, lit in literals(src)]
        self.assertEqual(sorted(got), sorted(["#1e293b", "rgba(15,23,42,0.5)", "qcolor(15,23,42)", "qt.white",
                                              "white", "white", "#fff"]))

    def test_no_colour_is_written_into_the_ui_code(self):
        bad = []
        unused = {p: set(d) for p, d in ALLOWED.items()}
        for path in _files():
            rel = os.path.relpath(path, ROOT).replace("\\", "/")
            with open(path, encoding="utf-8") as f:
                src = f.read()
            allowed = ALLOWED.get(rel, {})
            blocks = ALLOWED_BLOCKS.get(rel, {})
            if blocks:
                found = {n for n, _a, _b in _block_lines(ast.parse(src), set(blocks))}
                missing = set(blocks) - found
                self.assertFalse(missing, "%s: ALLOWED_BLOCKS names %s, which the file does not have" % (rel, missing))
            for line, lit in literals(src, blocks):
                if lit in allowed:
                    unused.get(rel, set()).discard(lit)
                    continue
                bad.append("%s:%d  %s" % (rel, line, lit))
        self.assertFalse(bad, "colours written into the UI code (use a theme token, or allow an identity colour "
                              "in tests/test_theme_colours.py ALLOWED with its reason):\n  " + "\n  ".join(bad))
        stale = ["%s  %s" % (p, lit) for p, lits in sorted(unused.items()) for lit in sorted(lits)]
        self.assertFalse(stale, "ALLOWED lists colours the code no longer has (remove them):\n  " + "\n  ".join(stale))

    def test_every_allowed_colour_says_why(self):
        for table in (ALLOWED, ALLOWED_BLOCKS):
            for path, lits in table.items():
                self.assertTrue(os.path.exists(os.path.join(ROOT, path)), path)
                for lit, why in lits.items():
                    self.assertTrue(isinstance(why, str) and len(why.strip()) >= 12, (path, lit))

    def test_a_block_is_only_its_own_lines(self):
        src = "\n".join(('PALETTE = {"light": "#ffffff",',
                         '           "dark": "#000000"}',
                         'OTHER = "#123456"',
                         'def logo():',
                         '    return QColor(217, 30, 24)', ''))
        self.assertEqual(literals(src, {"PALETTE", "logo"}), [(3, "#123456")])


if __name__ == "__main__":
    unittest.main()
