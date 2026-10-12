"""Dictionary page (Gusan 2.0): teach Gusan your names, words and fixes.

Mockup: Dictionary.dc.html (work.py dictionary, revision 2). "My words" are
chips with an add field (vocabulary.py: the term list every speech engine
and the AI step use), "Always replace" rows (text_cleanup's
cleanup_replacements), "More" folds out "Always remove"
(cleanup_custom_hallucinations). The rail has "Use my words with cloud
speech" (vocabulary_share_with_cloud; not Pro-only) and where the words work.
No Pro on this page.

Every change goes through app.set_setting / set_settings (instant apply).

Integration notes for Wave 4
----------------------------
* Words are written as {"vocabulary": [...], "initial_prompt": <mirror>} in
  one set_settings call (vocabulary.sync_config's rendering). Removing the
  last word clears initial_prompt too. A hand-written prose prompt from an
  old build shows as whatever vocabulary.load_terms makes of it and is
  replaced the first time the user edits the list (the old Settings box did
  the same). No side effect is needed: the decoder, cloud hint and LLM step
  read cfg on each use; text_cleanup reads the replace / remove lists per
  dictation.
* "Now spelled right:" quotes a recent dictation that contains one of the
  words (history.json, read on a worker via api_keys.run_then); hidden when
  there is none, and as soon as the word it shows is removed (the list
  changing - here or elsewhere - reloads it; only the newest answer is
  drawn). After a dictation is saved, Wave 4 may call
  ``page.reload_proof()``; on_open reloads anyway.
* The lists are rebuilt only when they really changed (one Add announces
  vocabulary + initial_prompt: two on_setting_changed calls).
* "Use my words with cloud speech" is disabled with "Off in Privacy mode"
  while privacy_mode is on (vocabulary._cloud_allowed already ignores it).
"""

import html
import logging
import re

from PySide6.QtCore import QEvent, QPoint, QRect, QSize, Qt
from PySide6.QtWidgets import (QBoxLayout, QFrame, QHBoxLayout, QLabel, QLayout, QLineEdit,
                               QPushButton, QSizePolicy, QVBoxLayout, QWidget)

from ui import theme
from ui.components import (Button, Card, Chip, IconButton, PageHeader, SettingRow, SettingsRail,
                           Toggle, icons, label)

logger = logging.getLogger("transcribe")

WATCHED = frozenset({
    "vocabulary", "initial_prompt", "cleanup_replacements", "cleanup_custom_hallucinations",
    "vocabulary_share_with_cloud", "privacy_mode",
})
WHERE = ("Dictation", "Audio to text", "Meetings")
NARROW_AT = 940          # below this the rail moves under the page (see smart_actions)
_SPLIT = re.compile(r"[,\n;]+")


# ── small helpers (pages stay independent, so these are local) ─────────────
def _alive(obj):
    try:
        import shiboken6
        return shiboken6.isValid(obj)
    except Exception:
        return True


def _cfg(app):
    cfg = getattr(app, "cfg", None)
    return cfg if isinstance(cfg, dict) else {}


def _set(app, key, value):
    fn = getattr(app, "set_setting", None)
    if not callable(fn):
        return False
    try:
        return fn(key, value) is not False
    except Exception:
        logger.warning("set_setting(%s) failed", key, exc_info=True)
        return False


def _set_many(app, changes):
    fn = getattr(app, "set_settings", None)
    if callable(fn):
        try:
            res = fn(dict(changes))
            return all(res.values()) if isinstance(res, dict) else res is not False
        except Exception:
            logger.warning("set_settings failed", exc_info=True)
            return False
    return all([_set(app, k, v) for k, v in changes.items()])


def _text(text, role, px, weight=400, parent=None, wrap=False):
    lb = label(text, role, parent, wrap=wrap)
    lb.setFont(theme.font(px, weight))
    return lb


def _icon_label(name, size, color, parent=None, sw=1.7):
    lb = QLabel(parent)
    lb.setPixmap(icons.pixmap(name, size, color, sw=sw))
    lb.setFixedSize(size, size)
    lb.setAttribute(Qt.WA_TransparentForMouseEvents, True)
    return lb


def _blend(token, over="#ffffff"):
    """A token's colour flattened over white (rich text has no alpha)."""
    a = theme.qcolor(token)
    b = theme.qcolor(over)
    k = a.alphaF()
    return "#%02x%02x%02x" % tuple(int(round(x * k + y * (1 - k)))
                                   for x, y in ((a.red(), b.red()), (a.green(), b.green()),
                                                (a.blue(), b.blue())))


def _plural(n, word, words=None):
    return "%d %s" % (n, word if n == 1 else (words or word + "s"))


# ── data (pure) ──────────────────────────────────────────────────────────────
def words(cfg):
    import vocabulary
    return vocabulary.load_terms(cfg)


def word_changes(terms):
    """{"vocabulary", "initial_prompt"} for a new term list (one set_settings)."""
    import vocabulary
    terms = vocabulary.normalize_terms(terms)
    tmp = {"vocabulary": terms}
    vocabulary.sync_config(tmp)
    return {"vocabulary": terms, "initial_prompt": tmp.get("initial_prompt", "")}


def check_new_words(existing, raw):
    """(words to add, problem text): the add field takes one word or several
    separated by commas. Problems are short and plain."""
    import vocabulary
    have = {t.casefold() for t in existing}
    add, problem = [], ""
    for piece in _SPLIT.split(raw or ""):
        term = " ".join(piece.split())
        if not term:
            continue
        if len(term) > vocabulary.MAX_TERM_CHARS:
            problem = "Keep each word under %d letters." % vocabulary.MAX_TERM_CHARS
            continue
        if len(term.split()) > vocabulary.MAX_TERM_WORDS:
            problem = "Up to %d words in one entry." % vocabulary.MAX_TERM_WORDS
            continue
        if term.casefold() in have:
            problem = problem or "“%s” is already there." % term
            continue
        have.add(term.casefold())
        add.append(term)
    room = vocabulary.MAX_TERMS - len(existing)
    if add and room <= 0:
        return [], "You can keep up to %d words." % vocabulary.MAX_TERMS
    if len(add) > room:
        add, problem = add[:room], "You can keep up to %d words." % vocabulary.MAX_TERMS
    return add, problem


def replacements(cfg):
    """[(from, to)] as text_cleanup reads them (a legacy dict too)."""
    raw = cfg.get("cleanup_replacements") or []
    if isinstance(raw, dict):
        raw = [{"from": k, "to": v} for k, v in raw.items()]
    out = []
    for item in raw if isinstance(raw, list) else []:
        if not isinstance(item, dict):
            continue
        src = str(item.get("from", "") or "").strip()
        if src:
            out.append((src, str(item.get("to", "") or "").strip()))
    return out


def _fold(text):
    try:
        import text_cleanup
        return text_cleanup.fold(text)
    except Exception:
        return " ".join(str(text or "").casefold().split())


def with_replacement(pairs, src, dst):
    """The list after adding src -> dst (an existing "When I hear" is updated)."""
    key = _fold(src)
    out = [(s, d) for s, d in pairs if _fold(s) != key]
    out.append((src.strip(), dst.strip()))
    return [{"from": s, "to": d} for s, d in out]


def phrases(cfg):
    raw = cfg.get("cleanup_custom_hallucinations") or []
    return [str(p).strip() for p in raw if isinstance(p, str) and str(p).strip()] if isinstance(raw, list) else []


# ── "Now spelled right:" ─────────────────────────────────────────────────────
def find_proof(entries, terms, max_chars=110):
    """The newest dictated sentence that spells one of the words right, or ""."""
    import vocabulary
    terms = [t for t in terms if t]
    if not terms:
        return ""
    for e in entries or []:
        text = str((e or {}).get("text") or "") if isinstance(e, dict) else ""
        if not text or not vocabulary.contains_any(text, terms):
            continue
        for sentence in re.split(r"(?<=[.!?])\s+", " ".join(text.split())):
            if any(re.search(r"(?<!\w)%s(?!\w)" % re.escape(t), sentence) for t in terms):
                if len(sentence) > max_chars:
                    sentence = sentence[:max_chars - 1].rstrip() + "…"
                return sentence
    return ""


def load_proof(terms):
    """Worker thread: read history.json and find a sentence."""
    import history
    return find_proof(history.load(), terms)


def proof_matches(sentence, terms):
    """The sentence still spells one of ``terms`` exactly (a removed word's
    sentence must not stay on screen)."""
    return any(t and re.search(r"(?<!\w)%s(?!\w)" % re.escape(t), sentence or "") for t in terms)


def proof_html(sentence, terms):
    """The sentence with each of the words marked (blue tint, as the mockup)."""
    if not sentence:
        return ""
    pattern = "|".join(re.escape(t) for t in sorted(set(terms), key=len, reverse=True) if t)
    if not pattern:
        return html.escape(sentence)
    mark = ('<span style="background-color:%s; color:%s; font-weight:600">&nbsp;%%s&nbsp;</span>'
            % (_blend("rgba(59,130,246,0.14)"), theme.qss_color("blue-tx")))
    out, pos = [], 0
    for m in re.finditer(r"(?<!\w)(?:%s)(?!\w)" % pattern, sentence):
        out.append(html.escape(sentence[pos:m.start()]))
        out.append(mark % html.escape(m.group(0)))
        pos = m.end()
    out.append(html.escape(sentence[pos:]))
    return "".join(out)


# ── widgets ──────────────────────────────────────────────────────────────────
class FlowLayout(QLayout):
    """Left-to-right rows that wrap (CSS flex-wrap)."""

    def __init__(self, parent=None, spacing=8):
        super().__init__(parent)
        self._items = []
        self._gap = spacing
        self.setContentsMargins(0, 0, 0, 0)

    def addItem(self, item):
        self._items.append(item)

    def count(self):
        return len(self._items)

    def itemAt(self, i):
        return self._items[i] if 0 <= i < len(self._items) else None

    def takeAt(self, i):
        return self._items.pop(i) if 0 <= i < len(self._items) else None

    def expandingDirections(self):
        return Qt.Orientations(0)

    def hasHeightForWidth(self):
        return True

    def heightForWidth(self, width):
        return self._do(QRect(0, 0, width, 0), True)

    def setGeometry(self, rect):
        super().setGeometry(rect)
        self._do(rect, False)

    def sizeHint(self):
        return self.minimumSize()

    def minimumSize(self):
        s = QSize()
        for it in self._items:
            s = s.expandedTo(it.minimumSize())
        m = self.contentsMargins()
        return s + QSize(m.left() + m.right(), m.top() + m.bottom())

    def _do(self, rect, test):
        m = self.contentsMargins()
        r = rect.adjusted(m.left(), m.top(), -m.right(), -m.bottom())
        x, y, line = r.x(), r.y(), 0
        for it in self._items:
            if it.widget() is not None and it.widget().isHidden():
                continue
            hint = it.sizeHint()
            nx = x + hint.width() + self._gap
            if nx - self._gap > r.right() + 1 and line > 0:
                x = r.x()
                y = y + line + self._gap
                nx = x + hint.width() + self._gap
                line = 0
            if not test:
                it.setGeometry(QRect(QPoint(x, y), hint))
            x = nx
            line = max(line, hint.height())
        return y + line - rect.y() + m.bottom()


class ElidedLabel(QLabel):
    """One line, cut with … to its width."""

    def __init__(self, text="", parent=None):
        super().__init__(parent)
        self._full = ""
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Fixed)
        self.setText(text)

    def setText(self, text):
        self._full = text or ""
        self.setAccessibleName(self._full)
        self.setToolTip(self._full if len(self._full) > 30 else "")
        QLabel.setText(self, self._full)
        self._elide()

    def full_text(self):
        return self._full

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._elide()

    def _elide(self):
        w = self.width()
        shown = self.fontMetrics().elidedText(self._full, Qt.ElideRight, w) if w > 0 else self._full
        if shown != QLabel.text(self):
            QLabel.setText(self, shown)

    def minimumSizeHint(self):
        return QSize(16, super().minimumSizeHint().height())


class WordChip(QFrame):
    """A word: white pill with ✕."""

    def __init__(self, word, on_remove, parent=None):
        super().__init__(parent)
        self.word = word
        self.setObjectName("wordChip")
        self.setStyleSheet("QFrame#wordChip { background: #ffffff; border: 1px solid %s; border-radius: 16px; }"
                           % theme.qss_color("line2"))
        self.setFixedHeight(32)
        self.setSizePolicy(QSizePolicy.Fixed, QSizePolicy.Fixed)
        lay = QHBoxLayout(self)
        lay.setContentsMargins(12, 0, 4, 0)
        lay.setSpacing(2)
        # The board's 500 renders as Segoe UI Variable's medium; Qt's Segoe UI has
        # no medium (500 falls back to regular), so semibold keeps the words the
        # page's heaviest text, as on the board.
        self.text_label = _text(word, "h3", 13.5, 600, self)
        lay.addWidget(self.text_label)
        self.remove_button = IconButton("x", "Remove %s" % word, size=24, icon_size=13, color="muted",
                                        parent=self)
        self.remove_button.setStyleSheet("QToolButton { border-radius: 12px; }")
        self.remove_button.clicked.connect(lambda: on_remove(word))
        lay.addWidget(self.remove_button)
        self.setAccessibleName(word)


class _ListRow(QFrame):
    """A table row with a hairline above it."""

    def __init__(self, parent=None, top_border=True, height=44):
        super().__init__(parent)
        self.setObjectName("dictRow")
        if top_border:
            self.setStyleSheet("QFrame#dictRow { border: none; border-top: 1px solid %s; }"
                               % theme.qss_color("line"))
        self.setFixedHeight(height)
        self.lay = QHBoxLayout(self)
        self.lay.setContentsMargins(0, 0, 0, 0)
        self.lay.setSpacing(14)


class ReplaceRow(_ListRow):
    def __init__(self, src, dst, on_remove, parent=None):
        super().__init__(parent)
        self.src, self.dst = src, dst
        self.src_label = ElidedLabel(src, self)
        theme.set_role(self.src_label, "body")
        self.src_label.setFont(theme.font(14))
        self.lay.addWidget(self.src_label, 1)
        self.lay.addWidget(_icon_label("arrow_r", 16, "faint", self))
        self.lay.addSpacing(2)
        self.dst_label = ElidedLabel(dst, self)
        theme.set_role(self.dst_label, "h3")
        self.dst_label.setFont(theme.font(14, 600))
        self.lay.addWidget(self.dst_label, 1)
        self.remove_button = IconButton("x", "Remove %s" % (dst or src), size=30, icon_size=14,
                                        color="muted", parent=self)
        self.remove_button.setAccessibleDescription("When I hear %s" % src)   # two rows may write the same
        self.remove_button.clicked.connect(lambda: on_remove(src, dst))
        self.lay.addWidget(self.remove_button)
        self.setAccessibleName("%s, write %s" % (src, dst))


class PhraseRow(_ListRow):
    def __init__(self, phrase, on_remove, top_border=True, parent=None):
        super().__init__(parent, top_border=top_border)
        self.phrase = phrase
        self.text_label = ElidedLabel(phrase, self)
        theme.set_role(self.text_label, "body")
        self.text_label.setFont(theme.font(14))
        self.lay.addWidget(self.text_label, 1)
        self.remove_button = IconButton("x", "Remove %s" % phrase, size=30, icon_size=14, color="muted",
                                        parent=self)
        self.remove_button.clicked.connect(lambda: on_remove(phrase))
        self.lay.addWidget(self.remove_button)


class MoreButton(QPushButton):
    """The folded "More · Always remove · 2 phrases" row (aria-expanded)."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("moreRow")
        self.setCheckable(True)
        self.setFixedHeight(48)
        self.setCursor(Qt.PointingHandCursor)
        self.setFocusPolicy(Qt.TabFocus)
        self.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        theme.focus_ring(self)
        self.setStyleSheet(
            "QPushButton#moreRow { background: %s; border: 1px solid %s; border-radius: 12px; text-align: left; }"
            "QPushButton#moreRow:hover { background: #ffffff; }"
            "QPushButton#moreRow[focusVisible=\"true\"] { border: 2px solid %s; }"
            % (theme.qss_color("rgba(255,255,255,0.55)"), theme.qss_color("glass-line"),
               theme.qss_color("blue-l")))
        lay = QHBoxLayout(self)
        lay.setContentsMargins(18, 0, 18, 0)
        lay.setSpacing(10)
        self.chev = _icon_label("chev_r", 16, "muted", self, sw=2)
        lay.addWidget(self.chev)
        t = _text("More", "label", 14, 600, self)
        t.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(t)
        s = _text("Always remove", "muted", 13, 400, self)
        s.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(s)
        lay.addStretch(1)
        self.count_label = _text("", "muted", 12.5, 400, self)
        self.count_label.setAttribute(Qt.WA_TransparentForMouseEvents, True)
        lay.addWidget(self.count_label)
        self.setAccessibleName("More: Always remove")
        self.toggled.connect(self._on_toggled)
        self._on_toggled(False)

    def _on_toggled(self, on):
        self.chev.setPixmap(icons.pixmap("chev_d" if on else "chev_r", 16, "muted", sw=2))
        self.setAccessibleDescription("Expanded" if on else "Collapsed")

    def set_count(self, n):
        self.count_label.setText(_plural(n, "phrase") if n else "")
        self.setAccessibleName("More: Always remove, %s" % _plural(n, "phrase") if n else "More: Always remove")


# ── the page ──────────────────────────────────────────────────────────────────
class DictionaryPage(QWidget):
    def __init__(self, main_app, parent=None):
        super().__init__(parent)
        self.app = main_app
        self.setProperty("gusan", "page")
        self._proof = ""
        self._proof_gen = 0              # a newer reload makes an older answer stale
        self._shown = {}                 # list name -> what is drawn (rebuild only on change)
        self._stacked = False
        self._watched_parent = None

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        self.header = PageHeader("Dictionary", parent=self)
        root.addWidget(self.header)
        self._body = QHBoxLayout()
        self._body.setContentsMargins(32, 0, 32, 28)
        self._body.setSpacing(24)
        main = QVBoxLayout()
        main.setSpacing(16)
        main.addWidget(self._build_words())
        main.addWidget(self._build_replace())
        self.more_button = MoreButton(self)
        self.more_button.toggled.connect(self._on_more)
        main.addWidget(self.more_button)
        main.addWidget(self._build_remove())
        main.addStretch(1)
        self._body.addLayout(main, 1)
        self.rail = self._build_rail()
        self._body.addWidget(self.rail, 0, Qt.AlignTop)
        root.addLayout(self._body, 1)

        self.refresh()
        self.reload_proof()

    # ── My words ──
    def _build_words(self):
        card = Card(self, pad=(20, 18), gap=14)
        card.setAccessibleName("My words")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label("My words", "h2", card))
        self.words_count = Chip("0 words", parent=card)
        head.addWidget(self.words_count)
        head.addStretch(1)
        card.add(head)

        add = QHBoxLayout()
        add.setSpacing(8)
        field = QFrame(card)
        field.setObjectName("wordField")
        field.setStyleSheet("QFrame#wordField { background: #ffffff; border: 1px solid %s; border-radius: 10px; }"
                            % theme.qss_color("blue-line"))
        field.setFixedHeight(44)
        fl = QHBoxLayout(field)
        fl.setContentsMargins(14, 0, 14, 0)
        fl.setSpacing(10)
        fl.addWidget(_icon_label("plus", 18, "blue-tx", field, sw=2))
        self.word_input = QLineEdit(field)
        self.word_input.setPlaceholderText("Add a name or word Gusan gets wrong")
        self.word_input.setAccessibleName("Add a word")
        self.word_input.setFont(theme.font(14.5))
        self.word_input.setStyleSheet("QLineEdit { border: none; background: transparent; padding: 0px; }")
        self.word_input.returnPressed.connect(self.add_words_from_input)
        self.word_input.textEdited.connect(lambda _t: self.word_error.hide())
        fl.addWidget(self.word_input, 1)
        add.addWidget(field, 1)
        self.add_word_button = Button("Add", "primary", "l", parent=card)
        self.add_word_button.setAccessibleName("Add word")
        self.add_word_button.clicked.connect(self.add_words_from_input)
        add.addWidget(self.add_word_button)
        card.add(add)
        self.word_error = label("", "error", card, wrap=True)
        self.word_error.hide()
        card.add(self.word_error)

        self.chips_box = QWidget(card)
        self.chips_flow = FlowLayout(self.chips_box, spacing=8)
        card.add(self.chips_box)
        self.words_empty = _text("No words yet.", "muted", 12.5, 400, card)
        card.add(self.words_empty)
        self.chips = []

        self.proof_row = QFrame(card)
        self.proof_row.setObjectName("proofRow")
        self.proof_row.setStyleSheet(
            "QFrame#proofRow { background: %s; border: 1px dashed %s; border-radius: 10px; }"
            % (theme.qss_color("rgba(255,255,255,0.65)"), theme.qss_color("line2")))
        pr = QHBoxLayout(self.proof_row)
        pr.setContentsMargins(14, 9, 14, 9)
        pr.setSpacing(8)
        pr.addWidget(_icon_label("check_circle", 16, "ok", self.proof_row))
        pr.addWidget(_text("Now spelled right:", "muted", 12.5, 400, self.proof_row))
        self.proof_label = _text("", "label", 13.5, 400, self.proof_row, wrap=True)
        self.proof_label.setTextFormat(Qt.RichText)
        # Wraps in a narrow window instead of being cut mid-word.
        self.proof_label.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        pr.addWidget(self.proof_label, 1)
        self.proof_row.hide()
        card.add(self.proof_row)
        return card

    # ── Always replace ──
    def _build_replace(self):
        card = Card(self, pad=(20, 18), gap=12)
        card.setAccessibleName("Always replace")
        head = QHBoxLayout()
        head.setSpacing(10)
        head.addWidget(label("Always replace", "h2", card))
        self.replace_count = Chip("0", parent=card)
        head.addWidget(self.replace_count)
        head.addStretch(1)
        card.add(head)

        table = QVBoxLayout()
        table.setSpacing(0)
        self.replace_head = QWidget(card)
        hh = QHBoxLayout(self.replace_head)
        hh.setContentsMargins(0, 0, 0, 6)
        hh.setSpacing(14)
        a = _text("When I hear", "muted", 12, 600, self.replace_head)
        a.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hh.addWidget(a, 1)
        hh.addSpacing(18 + 2)
        b = _text("Write", "muted", 12, 600, self.replace_head)
        b.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        hh.addWidget(b, 1)
        hh.addSpacing(30)
        table.addWidget(self.replace_head)
        self.replace_rows_box = QWidget(card)
        self.replace_rows_lay = QVBoxLayout(self.replace_rows_box)
        self.replace_rows_lay.setContentsMargins(0, 0, 0, 0)
        self.replace_rows_lay.setSpacing(0)
        table.addWidget(self.replace_rows_box)
        card.add(table)
        self.replace_rows = []

        add = QFrame(card)
        add.setObjectName("replaceAdd")
        self.replace_add = add
        al = QHBoxLayout(add)
        al.setContentsMargins(0, 12, 0, 0)
        al.setSpacing(10)
        self.when_input = QLineEdit(add)
        self.when_input.setPlaceholderText("When I hear…")
        self.when_input.setAccessibleName("When I hear")
        self.when_input.setFixedHeight(36)
        self.when_input.setFont(theme.font(13.5))
        self.when_input.returnPressed.connect(lambda: self.write_input.setFocus())
        al.addWidget(self.when_input, 1)
        al.addWidget(_icon_label("arrow_r", 16, "faint", add))
        self.write_input = QLineEdit(add)
        self.write_input.setPlaceholderText("Write…")
        self.write_input.setAccessibleName("Write")
        self.write_input.setFixedHeight(36)
        self.write_input.setFont(theme.font(13.5))
        self.write_input.returnPressed.connect(self.add_replacement_from_inputs)
        al.addWidget(self.write_input, 1)
        self.add_replace_button = Button("Add", "secondary", "m", icon="plus", parent=add)
        self.add_replace_button.setAccessibleName("Add replacement")
        self.add_replace_button.clicked.connect(self.add_replacement_from_inputs)
        al.addWidget(self.add_replace_button)
        card.add(add)
        self.replace_error = label("", "error", card, wrap=True)
        self.replace_error.hide()
        card.add(self.replace_error)
        for w in (self.when_input, self.write_input):
            w.textEdited.connect(lambda _t: self.replace_error.hide())
        return card

    # ── More: Always remove ──
    def _build_remove(self):
        card = Card(self, pad=(20, 8), gap=0)
        card.layout().setContentsMargins(20, 6, 20, 16)
        card.setAccessibleName("Always remove")
        self.remove_rows_box = QWidget(card)
        self.remove_rows_lay = QVBoxLayout(self.remove_rows_box)
        self.remove_rows_lay.setContentsMargins(0, 0, 0, 0)
        self.remove_rows_lay.setSpacing(0)
        card.add(self.remove_rows_box)
        self.remove_rows = []
        add = QHBoxLayout()
        add.setContentsMargins(0, 10, 0, 0)
        add.setSpacing(10)
        self.phrase_input = QLineEdit(card)
        self.phrase_input.setPlaceholderText("A phrase to always remove…")
        self.phrase_input.setAccessibleName("Phrase to always remove")
        self.phrase_input.setFixedHeight(36)
        self.phrase_input.setFont(theme.font(13.5))
        self.phrase_input.returnPressed.connect(self.add_phrase_from_input)
        self.phrase_input.textEdited.connect(lambda _t: self.remove_error.hide())
        add.addWidget(self.phrase_input, 1)
        self.add_phrase_button = Button("Add", "secondary", "m", icon="plus", parent=card)
        self.add_phrase_button.setAccessibleName("Add phrase")
        self.add_phrase_button.clicked.connect(self.add_phrase_from_input)
        add.addWidget(self.add_phrase_button)
        card.add(add)
        self.remove_error = label("", "error", card, wrap=True)
        self.remove_error.hide()
        card.add(self.remove_error)
        self.remove_card = card
        card.hide()
        return card

    # ── rail ──
    def _build_rail(self):
        rail = SettingsRail("Dictionary settings", parent=self)
        self.cloud_toggle = Toggle("Use my words with cloud speech", True, rail)
        self.cloud_toggle.toggled.connect(self._on_cloud_toggled)
        self.cloud_row = rail.add_row(SettingRow("Use my words with cloud speech", self.cloud_toggle))
        where = QWidget(rail)
        v = QVBoxLayout(where)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        for name in WHERE:
            h = QHBoxLayout()
            h.setSpacing(8)
            h.addWidget(_icon_label("check", 15, "ok", where, sw=2.2))
            h.addWidget(_text(name, "label", 13.5, 400, where))
            h.addStretch(1)
            v.addLayout(h)
        where.setAccessibleName("Where your words work: " + ", ".join(WHERE))
        self.where_row = rail.add_row(SettingRow("Where your words work", where, stacked=True))
        return rail

    # ── state ──
    def refresh(self):
        cfg = _cfg(self.app)
        rebuilt = [self._refresh_words(cfg), self._refresh_replacements(cfg), self._refresh_phrases(cfg)]
        if any(rebuilt):
            self._fix_tab_order()
        privacy = bool(cfg.get("privacy_mode"))
        self.cloud_toggle.blockSignals(True)
        self.cloud_toggle.setChecked(bool(cfg.get("vocabulary_share_with_cloud", True)) and not privacy)
        self.cloud_toggle.blockSignals(False)
        self.cloud_toggle.setEnabled(not privacy)
        self.cloud_toggle.update()
        self.cloud_row.set_hint("Off in Privacy mode" if privacy else "")

    def _changed(self, name, value):
        """True (and remembered) when ``value`` differs from what is drawn."""
        if self._shown.get(name) == value and name in self._shown:
            return False
        self._shown[name] = value
        return True

    def _fix_tab_order(self):
        """Rows built after the page join the end of the Tab chain (after the
        rail): put every ✕ back where it is on screen."""
        order = [self.word_input, self.add_word_button]
        order += [c.remove_button for c in self.chips]
        order += [r.remove_button for r in self.replace_rows]
        order += [self.when_input, self.write_input, self.add_replace_button, self.more_button]
        order += [r.remove_button for r in self.remove_rows]
        order += [self.phrase_input, self.add_phrase_button, self.cloud_toggle]
        for a, b in zip(order, order[1:]):
            QWidget.setTabOrder(a, b)

    def _refresh_words(self, cfg):
        """Redraw the chips when the list changed; True when they were rebuilt."""
        terms = words(cfg)
        before = self._shown.get("words")
        if not self._changed("words", list(terms)):
            self._show_proof(terms)
            return False
        for chip in self.chips:
            self.chips_flow.removeWidget(chip)
            chip.hide()
            chip.deleteLater()
        self.chips = []
        for t in terms:
            chip = WordChip(t, self.remove_word, self.chips_box)
            self.chips_flow.addWidget(chip)
            chip.show()
            self.chips.append(chip)
        self.chips_box.setVisible(bool(terms))
        self.words_empty.setVisible(not terms)
        self.words_count.setText(_plural(len(terms), "word"))
        self.words_count.setVisible(bool(terms))         # "No words yet." says it already
        self.chips_flow.invalidate()
        self._show_proof(terms)
        if before is not None:
            self.reload_proof()                          # the list changed (here or elsewhere)
        return True

    def _refresh_replacements(self, cfg):
        pairs = replacements(cfg)
        if not self._changed("replace", list(pairs)):
            return False
        for row in self.replace_rows:
            self.replace_rows_lay.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.replace_rows = []
        for src, dst in pairs:
            row = ReplaceRow(src, dst, self.remove_replacement, self.replace_rows_box)
            self.replace_rows_lay.addWidget(row)
            self.replace_rows.append(row)
        self.replace_head.setVisible(bool(pairs))
        self.replace_rows_box.setVisible(bool(pairs))
        self.replace_count.setText(str(len(pairs)))
        self.replace_count.setVisible(bool(pairs))
        # The hairline above the add row only separates it from rows.
        self.replace_add.setStyleSheet(
            "QFrame#replaceAdd { border: none; border-top: 1px solid %s; }" % theme.qss_color("line")
            if pairs else "QFrame#replaceAdd { border: none; }")
        self.replace_add.layout().setContentsMargins(0, 12 if pairs else 0, 0, 0)
        return True

    def _refresh_phrases(self, cfg):
        items = phrases(cfg)
        if not self._changed("remove", list(items)):
            return False
        for row in self.remove_rows:
            self.remove_rows_lay.removeWidget(row)
            row.hide()
            row.deleteLater()
        self.remove_rows = []
        for i, p in enumerate(items):
            row = PhraseRow(p, self.remove_phrase, top_border=i > 0, parent=self.remove_rows_box)
            self.remove_rows_lay.addWidget(row)
            self.remove_rows.append(row)
        self.remove_rows_box.setVisible(bool(items))
        self.more_button.set_count(len(items))
        return True

    # ── words ──
    def add_words_from_input(self):
        cfg = _cfg(self.app)
        existing = words(cfg)
        add, problem = check_new_words(existing, self.word_input.text())
        if add:
            if _set_many(self.app, word_changes(existing + add)):
                self.word_input.clear()
            else:
                problem = problem or "Couldn't save that. Try again."
        if problem:
            self.word_error.setText(problem)
            self.word_error.show()
        else:
            self.word_error.hide()
        self.refresh()                      # a changed list reloads the proof sentence
        self.word_input.setFocus()

    def _focused_index(self, buttons):
        """Which of ``buttons`` has the keyboard focus (None: none)."""
        for i, b in enumerate(buttons):
            if b.hasFocus():
                return i
        return None

    def _refocus(self, index, buttons, fallback):
        """After a keyboard removal: the next ✕ (or ``fallback``) takes the focus,
        so Tab users keep their place instead of landing at the window's start."""
        if index is None:
            return
        target = buttons[min(index, len(buttons) - 1)] if buttons else fallback
        target.setFocus(Qt.TabFocusReason)

    def remove_word(self, word):
        idx = self._focused_index([c.remove_button for c in self.chips])
        terms = [t for t in words(_cfg(self.app)) if t != word]
        _set_many(self.app, word_changes(terms))
        self.refresh()
        self._refocus(idx, [c.remove_button for c in self.chips], self.word_input)

    # ── replacements ──
    def add_replacement_from_inputs(self):
        src = " ".join(self.when_input.text().split())
        dst = " ".join(self.write_input.text().split())
        if not src:
            self._replace_problem("Type what you hear first.")
            self.when_input.setFocus()
            return
        if not dst:
            self._replace_problem("Type what to write.")
            self.write_input.setFocus()
            return
        pairs = replacements(_cfg(self.app))
        if _set(self.app, "cleanup_replacements", with_replacement(pairs, src, dst)):
            self.when_input.clear()
            self.write_input.clear()
            self.replace_error.hide()
            self.when_input.setFocus()
        else:
            self._replace_problem("Couldn't save that. Try again.")
        self.refresh()

    def _replace_problem(self, text):
        self.replace_error.setText(text)
        self.replace_error.show()

    def remove_replacement(self, src, dst):
        """Remove the row by what it says (not its position: the list may have
        changed under it)."""
        idx = self._focused_index([r.remove_button for r in self.replace_rows])
        pairs = replacements(_cfg(self.app))
        keep = [(s, d) for s, d in pairs if (s, d) != (src, dst)]
        if len(keep) != len(pairs):
            _set(self.app, "cleanup_replacements", [{"from": s, "to": d} for s, d in keep])
        self.refresh()
        self._refocus(idx, [r.remove_button for r in self.replace_rows], self.when_input)

    # ── always remove ──
    def _on_more(self, on):
        self.remove_card.setVisible(on)
        if on:
            self.phrase_input.setFocus()

    def add_phrase_from_input(self):
        p = " ".join(self.phrase_input.text().split())
        if not p:
            return
        items = phrases(_cfg(self.app))
        if _fold(p) in {_fold(x) for x in items}:
            self.remove_error.setText("Already there.")
            self.remove_error.show()
            return
        if _set(self.app, "cleanup_custom_hallucinations", items + [p]):
            self.phrase_input.clear()
            self.remove_error.hide()
        else:
            self.remove_error.setText("Couldn't save that. Try again.")
            self.remove_error.show()
        self.refresh()

    def remove_phrase(self, phrase):
        idx = self._focused_index([r.remove_button for r in self.remove_rows])
        items = [x for x in phrases(_cfg(self.app)) if x != phrase]
        _set(self.app, "cleanup_custom_hallucinations", items)
        self.refresh()
        self._refocus(idx, [r.remove_button for r in self.remove_rows], self.phrase_input)

    # ── rail ──
    def _on_cloud_toggled(self, on):
        if not _set(self.app, "vocabulary_share_with_cloud", bool(on)):
            self.refresh()

    # ── "Now spelled right:" ──
    def reload_proof(self):
        """Find the sentence on a worker; only the newest reload's answer is drawn."""
        self._proof_gen += 1
        gen = self._proof_gen
        terms = words(_cfg(self.app))
        if not terms:
            self.set_proof("")
            return
        try:
            import api_keys
            api_keys.run_then(load_proof, terms, then=lambda s, g=gen: self._on_proof(s, g),
                              error=lambda _e, g=gen: self._on_proof("", g))
        except Exception:
            logger.debug("proof sentence could not load", exc_info=True)

    def _on_proof(self, sentence, gen=None):
        if _alive(self) and (gen is None or gen == self._proof_gen):
            self.set_proof(sentence or "")

    def set_proof(self, sentence):
        self._proof = sentence or ""
        self._show_proof(words(_cfg(self.app)))

    def _show_proof(self, terms):
        ok = bool(terms) and proof_matches(self._proof, terms)
        text = proof_html(self._proof, terms) if ok else ""
        self.proof_label.setText(text)
        self.proof_label.setAccessibleName(self._proof if text else "")
        self.proof_row.setVisible(bool(text))

    # ── responsive: rail beside the page, or under it when narrow ──
    def _available_width(self):
        p = self.parentWidget()
        return p.width() if p is not None and p.width() > 0 else self.width()

    def _relayout(self):
        stacked = self._available_width() < NARROW_AT
        if stacked == self._stacked:
            return
        self._stacked = stacked
        self._body.setDirection(QBoxLayout.TopToBottom if stacked else QBoxLayout.LeftToRight)
        self._body.setAlignment(self.rail, (Qt.AlignLeft | Qt.AlignTop) if stacked else Qt.AlignTop)

    def is_stacked(self):
        return self._stacked

    def eventFilter(self, obj, ev):
        if obj is self._watched_parent and ev.type() == QEvent.Resize:
            self._relayout()
        return False

    def showEvent(self, e):
        p = self.parentWidget()
        if p is not None and p is not self._watched_parent:
            p.installEventFilter(self)
            self._watched_parent = p
        self._relayout()
        super().showEvent(e)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._relayout()

    # ── shell hooks ──
    def on_open(self, **_opts):
        self.refresh()
        self.reload_proof()

    def on_setting_changed(self, key):
        if key in WATCHED:
            self.refresh()


def create(main_app):
    return DictionaryPage(main_app)
