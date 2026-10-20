"""The Dictionary is a section of Settings, not a page (ui/pages/settings.py embeds
ui/pages/dictionary.py with embedded=True): the section list and its order, the
lazy build, the aliases, the embedded page's layout, the whole behaviour inside
Settings (words, replacements, always-remove phrases, the cloud toggle, redraws
from config changes, the proof sentence) and a Dictionary that fails to build."""
import time

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import (QAbstractButton, QApplication, QComboBox, QLabel, QLineEdit, QSizePolicy,
                               QVBoxLayout, QWidget)

from tests_qt.conftest import make_stub_app

WORDS = ["Aram", "Aibuben", "PySide6"]
LAZY = ["speech", "dictionary", "privacy", "updates"]
DICT_KEYS = ("vocabulary", "initial_prompt", "cleanup_replacements", "cleanup_custom_hallucinations",
             "vocabulary_share_with_cloud", "privacy_mode")


# ── helpers ──────────────────────────────────────────────────────────────────
def _wait(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while not cond() and time.monotonic() < end:
        QApplication.processEvents()
        time.sleep(0.01)
    return cond()


def _spin(n=5):
    for _ in range(n):
        QApplication.processEvents()


def dict_app(tier="free", **cfg):
    """make_stub_app whose set_setting / set_settings really write cfg (like the
    controller) and record what they were asked, in order, in ``app.calls``."""
    app = make_stub_app(tier)
    app.cfg.update(cfg)

    def set_settings(changes):
        app.calls.append(("set_settings", dict(changes)))
        app.cfg.update(changes)
        return {k: True for k in changes}

    def set_setting(key, value):
        app.calls.append(("set_setting", key, value))
        app.cfg[key] = value
        return True
    app.set_settings = set_settings
    app.set_setting = set_setting
    return app


def writes(app):
    """Every setting write so far (tracking and show_* calls left out)."""
    return [c for c in app.calls if c[0] in ("set_setting", "set_settings")]


@pytest.fixture(autouse=True)
def quiet(qapp, monkeypatch):
    """No real window fitting, autostart probe, browser, or history read; the
    proof sentence is found synchronously (``quiet.sentence``, ``quiet.calls``)."""
    import api_keys
    import ui.pages.dictionary as dct
    import ui.winfit
    from ui.pages import settings as S
    monkeypatch.setattr(ui.winfit, "settle_on_screen", lambda *a, **k: None)
    monkeypatch.setattr(S, "autostart_state", lambda: (True, True))
    monkeypatch.setattr(S, "open_url", lambda url: None)
    state = type("Proof", (), {"sentence": "", "calls": []})

    def fake_load_proof(terms):
        state.calls.append(list(terms))
        return state.sentence
    monkeypatch.setattr(dct, "load_proof", fake_load_proof)
    real = api_keys.run_then

    def run_then(fn, *args, then=None, error=None, **kw):
        if fn is fake_load_proof:
            then(fn(*args))
            return None
        return real(fn, *args, then=then, error=error, **kw)
    monkeypatch.setattr(api_keys, "run_then", run_then)
    state.calls = []
    return state


@pytest.fixture
def make(qapp, monkeypatch):
    """make(tier, width=1100, height=800, show=False, idle=False, **cfg) -> (SettingsPage, app).
    The idle build of the lazy sections (one per event-loop turn) is off unless
    ``idle=True``, so a test decides when each section appears."""
    made = []

    def build(tier="free", width=1100, height=800, show=False, idle=False, **cfg):
        from ui.pages.settings import SettingsPage
        if not idle:
            monkeypatch.setattr(SettingsPage, "_build_next", lambda self: None)
        app = dict_app(tier, **cfg)
        page = SettingsPage(app)
        page.resize(width, height)
        if show:
            page.show()
        made.append(page)
        return page, app

    yield build
    for p in made:
        p.hide()
        p.deleteLater()
    _spin()


@pytest.fixture
def built(make):
    """A Settings page with every section built, the Dictionary holding some data."""
    def build(**cfg):
        base = dict(vocabulary=list(WORDS), initial_prompt="Glossary: Aram, Aibuben, PySide6.",
                    cleanup_replacements=[{"from": "goo san", "to": "Gusan"}],
                    cleanup_custom_hallucinations=["thanks for watching"])
        base.update(cfg)
        page, app = make(**base)
        page.ensure_built()
        _spin()
        app.calls.clear()
        return page, app
    return build


@pytest.fixture
def env(qapp, make):
    """The real shell around Settings (for routing and scrolling)."""
    made = []

    def build(tier="free", **cfg):
        from ui.shell import MainWindow
        app = dict_app(tier, **cfg)
        win = MainWindow(app)
        win.resize(1280, 820)
        made.append(win)
        return win, app

    yield type("Env", (), {"build": staticmethod(build)})
    for w in made:
        w.hide()
        w.deleteLater()
    _spin()


def _unnamed(widget):
    out = []
    for b in widget.findChildren(QAbstractButton):
        if not (b.text() or "").strip() and not (b.accessibleName() or "").strip():
            out.append("%s(%s)" % (type(b).__name__, b.objectName()))
    for f in widget.findChildren(QComboBox) + widget.findChildren(QLineEdit):
        if isinstance(f, QLineEdit) and isinstance(f.parentWidget(), QComboBox):
            continue
        if not (f.accessibleName() or "").strip():
            out.append(type(f).__name__)
    return out


# ── the section list ─────────────────────────────────────────────────────────
def test_sections_order_and_icons():
    from ui.pages.settings import SECTION_IDS, SECTIONS
    assert SECTIONS == (("general", "gear", "General"), ("microphone", "mic", "Microphone"),
                        ("speech", "wave", "Speech & AI"), ("dictionary", "book", "Dictionary"),
                        ("privacy", "shield", "Privacy"), ("updates", "refresh", "Updates & about"))
    assert SECTION_IDS == ("general", "microphone", "speech", "dictionary", "privacy", "updates")
    assert dict((s, i) for s, i, _t in SECTIONS)["dictionary"] == "book"


def test_the_section_list_has_dictionary_between_speech_and_privacy(make):
    page, _app = make()
    nav = page.nav
    assert list(nav.items) == ["general", "microphone", "speech", "dictionary", "privacy", "updates"]
    lay = nav.layout()
    order = [lay.indexOf(nav.items[s]) for s in nav.items]
    assert order == sorted(order) and len(set(order)) == 6
    item = nav.items["dictionary"]
    assert item.text_label.text() == "Dictionary" and item._icon_name == "book"
    assert item.accessibleName() == "Dictionary" and item.isEnabled()
    assert page.current_section() == "general"
    assert item.accessibleDescription() == ""


def test_clicking_the_dictionary_item_shows_the_section(make):
    page, _app = make(show=True)
    _spin()
    assert not page.is_built("dictionary") and page.dictionary is None
    page.nav.items["dictionary"].click()
    _spin()
    assert page.current_section() == "dictionary"
    assert page.is_built("speech") and page.is_built("dictionary") and not page.is_built("privacy")
    assert page.dictionary is not None
    assert page.nav.items["dictionary"].accessibleDescription() == "Current section"
    assert all(page.nav.items[s].accessibleDescription() == "" for s in page.nav.items if s != "dictionary")
    # Back to another section and on to the next one.
    page.nav.items["privacy"].click()
    _spin()
    assert page.current_section() == "privacy" and page.is_built("privacy")
    assert page.nav.items["dictionary"].accessibleDescription() == ""


def test_the_dictionary_is_no_feature_page_link(make):
    from ui.pages.settings import FEATURE_PAGES
    from ui.shell import ROUTES
    page, _app = make()
    routes = [r for _i, _n, r in FEATURE_PAGES]
    assert routes == ["dictation", "smart_actions", "meetings", "live_assistant", "audio_to_text"]
    assert "dictionary" not in routes
    assert list(page.feature_links) == routes
    assert all(r in ROUTES for r in page.feature_links)
    assert "dictionary" not in page.feature_links
    assert [b.accessibleName() for b in page.feature_links.values()] == [
        "Open Dictation", "Open Smart Actions", "Open Meetings", "Open Live Assistant", "Open Audio to text"]
    assert not any("Dictionary" in b.text() for b in page.feature_links.values())


# ── lazy build ───────────────────────────────────────────────────────────────
def test_the_dictionary_is_built_lazily_between_speech_and_privacy(make):
    page, _app = make()
    # Nothing has had an event-loop turn yet: only General and Microphone exist.
    assert page._lazy == LAZY
    assert page.dictionary is None and not page.is_built("dictionary")
    assert page.section_widget("dictionary") is None
    assert page.is_built("general") and page.is_built("microphone")
    seen = []
    for sid in LAZY:
        page._build_one()
        seen.append(sid)
        assert [s for s in LAZY if page.is_built(s)] == seen
        assert (page.dictionary is not None) == ("dictionary" in seen)
    assert page._lazy == []
    ys = [page.col.indexOf(page.section_widget(s)) for s in ("general", "microphone", "speech", "dictionary",
                                                          "privacy", "updates")]
    assert ys == sorted(ys) and len(set(ys)) == 6, ys              # on screen in the nav's order


def test_ensure_built_up_to_the_dictionary_stops_there(make):
    page, _app = make()
    page.ensure_built("dictionary")
    assert page.is_built("speech") and page.is_built("dictionary")
    assert not page.is_built("privacy") and not page.is_built("updates")
    assert page._lazy == ["privacy", "updates"]
    first = page.dictionary
    page.ensure_built("dictionary")                                # again: nothing more, same page
    assert page.dictionary is first and page._lazy == ["privacy", "updates"]
    page.ensure_built()
    assert page._lazy == [] and all(page.is_built(s) for s in LAZY)
    assert page.dictionary is first


def test_the_idle_build_reaches_the_dictionary(make):
    page, _app = make(show=True, idle=True)
    assert _wait(lambda: page.is_built("dictionary"), 30)
    assert page.dictionary is not None
    assert _wait(lambda: page.is_built("updates"), 30)


@pytest.mark.parametrize("alias", ["dictionary", "words", "vocabulary", "replacements"])
def test_show_section_and_its_aliases_build_the_dictionary_first(make, alias):
    page, _app = make(show=True)
    assert page.dictionary is None
    page.show_section(alias)
    assert page.is_built("dictionary") and page.dictionary is not None
    assert not page.is_built("privacy")
    assert page.current_section() == "dictionary"
    assert page.section_widget(alias) is page.section_widget("dictionary")
    assert page.section_widget("dictionary").accessibleName() == "Dictionary"


@pytest.mark.parametrize("alias", ["dictionary", "words", "vocabulary", "replacements"])
def test_on_open_with_the_dictionary_section_lands_on_it(make, alias):
    page, _app = make(show=True, idle=True)
    assert page.dictionary is None
    page.on_open(section=alias)
    assert page.opts == {"section": alias}
    assert page.dictionary is not None and page.is_built("dictionary")
    assert page.current_section() == "dictionary"
    # It scrolls there; once Privacy and Updates exist the page is long enough
    # to bring the section to the top (the same settle as for a jump near the end).
    bar = page.scroll.verticalScrollBar()
    assert _wait(lambda: page.is_built("updates"), 30)
    assert _wait(lambda: bar.value() == page.section_widget("dictionary").y(), 10),         (bar.value(), page.section_widget("dictionary").y(), bar.maximum())
    assert bar.value() > 0 and page.current_section() == "dictionary"


def test_the_section_has_one_title_and_a_name(built):
    page, _app = built()
    sec = page.section_widget("dictionary")
    assert sec.accessibleName() == "Dictionary"
    assert page.dictionary.parentWidget() is sec
    titles = [lb for lb in sec.findChildren(QLabel) if lb.text() == "Dictionary"]
    assert len(titles) == 1                                         # no second page title inside the section
    assert sec.layout().itemAt(0).widget() is titles[0]
    assert sec.layout().itemAt(1).widget() is page.dictionary


def test_every_dictionary_control_has_a_name(built):
    page, _app = built()
    page.dictionary.more_button.click()
    assert not _unnamed(page.dictionary), _unnamed(page.dictionary)
    for w in page.dictionary.findChildren(QAbstractButton) + page.dictionary.findChildren(QLineEdit):
        fp = w.focusPolicy()
        assert int(getattr(fp, "value", fp)) & 1, (w, w.accessibleName())          # reachable with Tab


# ── the embedded page's shape ────────────────────────────────────────────────
def test_the_embedded_page_has_no_header_no_margins_and_a_wide_rail(built):
    from ui.components import PageHeader
    from ui.pages.dictionary import DictionaryPage
    page, _app = built()
    d = page.dictionary
    assert isinstance(d, DictionaryPage) and d._embedded is True
    assert d.header is None and d.findChildren(PageHeader) == []
    m = d._body.contentsMargins()
    assert (m.left(), m.top(), m.right(), m.bottom()) == (0, 0, 0, 0)
    rail = d.rail
    assert rail.title_label.text() == "Dictionary settings"
    assert rail.minimumWidth() == 0 and rail.maximumWidth() == 16777215
    assert rail.sizePolicy().horizontalPolicy() == QSizePolicy.Expanding
    assert d.is_stacked() is True                                   # from construction on, shown or not
    assert d._body.direction() == d._body.Direction.TopToBottom
    assert d.parentWidget() is page.section_widget("dictionary")


@pytest.mark.parametrize("width", [400, 700, 1000, 1400])
def test_the_rail_stays_under_the_lists_at_every_width(make, width):
    page, _app = make(width=width, show=True, vocabulary=list(WORDS))
    page.ensure_built()
    d = page.dictionary
    assert d.is_stacked()                                           # even before the first layout pass
    _spin(10)
    assert d.is_stacked()
    page.resize(width + 7, 800)
    _spin(5)
    assert d.is_stacked()
    d.layout().activate()
    # Under the lists (Always remove is folded away), starting at the left edge, as wide as the lists.
    assert d.rail.x() == 0 and d.rail.width() == d.width()
    assert d.rail.y() >= d.more_button.y() + d.more_button.height()
    assert d.rail.y() > d.word_input.mapTo(d, d.word_input.rect().topLeft()).y()
    # Nothing in the section is wider than the section, and the Dictionary never
    # asks for more width than the other sections do (so it cannot be what makes
    # the page scroll sideways; the page's own minimum comes from Updates / General).
    assert d.width() <= page.section_widget("dictionary").width()
    others = [sec.minimumSizeHint().width() for sid, sec in page._sections.items() if sid != "dictionary"]
    assert d.minimumSizeHint().width() <= min(others)


def test_the_rail_is_wider_than_the_standalone_one_in_a_wide_settings_page(make):
    page, _app = make(width=1500, show=True)
    page.ensure_built()
    _spin(5)
    assert page.dictionary.rail.width() > 316
    assert page.dictionary.rail.width() == page.dictionary.width()


def test_the_standalone_page_is_unchanged(qapp):
    import ui.pages.dictionary as dct
    from ui.components import PageHeader
    app = dict_app("free")
    page = dct.create(app)
    assert page._embedded is False
    assert isinstance(page.header, PageHeader) and page.header.title_label.text() == "Dictionary"
    m = page._body.contentsMargins()
    assert (m.left(), m.top(), m.right(), m.bottom()) == (32, 0, 32, 28)
    assert page.rail.minimumWidth() == 316 and page.rail.maximumWidth() == 316
    assert page.rail.sizePolicy().horizontalPolicy() == QSizePolicy.Fixed
    assert dct.NARROW_AT == 940
    host = QWidget()
    lay = QVBoxLayout(host)
    lay.setContentsMargins(0, 0, 0, 0)
    page.setParent(host)
    lay.addWidget(page)
    host.resize(1100, 900)
    host.show()
    try:
        _spin(5)
        assert not page.is_stacked() and page.rail.width() == 316        # beside the lists
        host.resize(939, 900)
        assert _wait(page.is_stacked)
        assert page.rail.width() == 316                                   # still the fixed panel, under them
        host.resize(940, 900)
        assert _wait(lambda: not page.is_stacked())
    finally:
        host.hide()
        host.deleteLater()
        _spin()


# ── behaviour inside Settings: My words ──────────────────────────────────────
def test_add_one_word(built):
    page, app = built()
    d = page.dictionary
    assert [c.word for c in d.chips] == WORDS and d.words_count.text() == "3 words"
    d.word_input.setText("Kubernetes")
    d.add_word_button.click()
    assert writes(app) == [("set_settings", {
        "vocabulary": WORDS + ["Kubernetes"],
        "initial_prompt": "Glossary: Aram, Aibuben, PySide6, Kubernetes."})]
    assert [c.word for c in d.chips] == WORDS + ["Kubernetes"] and d.words_count.text() == "4 words"
    assert d.word_input.text() == "" and d.word_error.isHidden()


def test_add_with_the_enter_key(built):
    page, app = built()
    d = page.dictionary
    d.word_input.setText("Kubernetes")
    QTest.keyClick(d.word_input, Qt.Key_Return)
    assert writes(app)[0][1]["vocabulary"] == WORDS + ["Kubernetes"]
    assert [c.word for c in d.chips][-1] == "Kubernetes"


def test_add_several_words_separated_by_commas_semicolons_and_new_lines(built):
    page, app = built(vocabulary=[], initial_prompt="")
    d = page.dictionary
    assert d.chips == [] and not d.words_empty.isHidden() and d.words_count.isHidden()
    d.word_input.setText("Anna, David\nEmma;  Hakobyan Mkrtchyan ,,")
    d.add_word_button.click()
    assert writes(app) == [("set_settings", {
        "vocabulary": ["Anna", "David", "Emma", "Hakobyan Mkrtchyan"],
        "initial_prompt": "Glossary: Anna, David, Emma, Hakobyan Mkrtchyan."})]
    assert [c.word for c in d.chips] == ["Anna", "David", "Emma", "Hakobyan Mkrtchyan"]
    assert d.words_empty.isHidden() and not d.chips_box.isHidden() and d.words_count.text() == "4 words"


def test_duplicate_words_are_refused_with_a_plain_message(built):
    page, app = built()
    d = page.dictionary
    d.word_input.setText("pyside6")
    d.add_word_button.click()
    assert writes(app) == []
    assert not d.word_error.isHidden() and d.word_error.text() == "“pyside6” is already there."
    assert d.word_input.text() == "pyside6"                         # kept, so the user can fix it
    d.word_input.textEdited.emit("pyside")                          # typing clears the message
    assert d.word_error.isHidden()
    # A duplicate next to a new word: the new one is added, the message says why the other was not.
    d.word_input.setText("Anna, ARAM, anna")
    d.add_word_button.click()
    assert writes(app) == [("set_settings", {"vocabulary": WORDS + ["Anna"],
                                              "initial_prompt": "Glossary: Aram, Aibuben, PySide6, Anna."})]
    assert not d.word_error.isHidden() and "already there" in d.word_error.text()
    assert [c.word for c in d.chips] == WORDS + ["Anna"]


def test_a_too_long_word_is_refused(built):
    page, app = built()
    d = page.dictionary
    d.word_input.setText("x" * 60)
    d.add_word_button.click()
    assert writes(app) == []
    assert not d.word_error.isHidden() and d.word_error.text() == "Keep each word under 50 letters."
    assert [c.word for c in d.chips] == WORDS
    d.word_input.setText("one two three four five six seven")
    d.add_word_button.click()
    assert writes(app) == [] and d.word_error.text() == "Up to 6 words in one entry."
    d.word_input.setText("x" * 60 + ", Anna")                       # the good one still goes in
    d.add_word_button.click()
    assert writes(app)[0][1]["vocabulary"] == WORDS + ["Anna"]
    assert d.word_error.text() == "Keep each word under 50 letters."


def test_an_empty_field_does_nothing(built):
    page, app = built()
    d = page.dictionary
    d.word_input.setText("  , ; ")
    d.add_word_button.click()
    assert writes(app) == [] and d.word_error.isHidden()


def test_a_failed_save_keeps_the_text_and_says_so(built):
    page, app = built()
    app.set_settings = lambda changes: {k: False for k in changes}
    d = page.dictionary
    d.word_input.setText("Anna")
    d.add_word_button.click()
    assert d.word_input.text() == "Anna" and d.word_error.text() == "Couldn't save that. Try again."
    assert [c.word for c in d.chips] == WORDS


def test_remove_a_word(built):
    page, app = built()
    d = page.dictionary
    assert d.chips[1].remove_button.accessibleName() == "Remove Aibuben"
    d.chips[1].remove_button.click()
    assert writes(app) == [("set_settings", {"vocabulary": ["Aram", "PySide6"],
                                              "initial_prompt": "Glossary: Aram, PySide6."})]
    assert [c.word for c in d.chips] == ["Aram", "PySide6"] and d.words_count.text() == "2 words"
    d.chips[0].remove_button.click()
    d.chips[0].remove_button.click()
    assert writes(app)[-1] == ("set_settings", {"vocabulary": [], "initial_prompt": ""})
    assert d.chips == [] and not d.words_empty.isHidden() and d.chips_box.isHidden() and d.words_count.isHidden()


# ── Always replace ───────────────────────────────────────────────────────────
def test_add_and_update_a_replacement(built):
    page, app = built()
    d = page.dictionary
    assert len(d.replace_rows) == 1 and d.replace_count.text() == "1"
    assert d.replace_rows[0].remove_button.accessibleName() == "Remove Gusan"
    d.when_input.setText("pie side six")
    d.write_input.setText("PySide6")
    d.add_replace_button.click()
    assert writes(app) == [("set_setting", "cleanup_replacements", [
        {"from": "goo san", "to": "Gusan"}, {"from": "pie side six", "to": "PySide6"}])]
    assert len(d.replace_rows) == 2 and d.replace_count.text() == "2"
    assert d.when_input.text() == "" and d.write_input.text() == "" and d.replace_error.isHidden()
    d.when_input.setText("GOO  san")                                # same "When I hear": updated, not doubled
    d.write_input.setText("Gusan app")
    QTest.keyClick(d.write_input, Qt.Key_Return)
    assert writes(app)[-1] == ("set_setting", "cleanup_replacements", [
        {"from": "pie side six", "to": "PySide6"}, {"from": "GOO san", "to": "Gusan app"}])
    assert len(d.replace_rows) == 2


def test_an_incomplete_replacement_is_refused(built):
    page, app = built()
    d = page.dictionary
    d.write_input.setText("PySide6")
    d.add_replace_button.click()
    assert writes(app) == [] and d.replace_error.text() == "Type what you hear first."
    d.when_input.setText("pie side six")
    d.write_input.setText("")
    d.add_replace_button.click()
    assert writes(app) == [] and d.replace_error.text() == "Type what to write."
    assert not d.replace_error.isHidden() and len(d.replace_rows) == 1


def test_remove_a_replacement(built):
    page, app = built()
    d = page.dictionary
    d.replace_rows[0].remove_button.click()
    assert writes(app) == [("set_setting", "cleanup_replacements", [])]
    assert d.replace_rows == [] and d.replace_count.isHidden()


# ── More > Always remove ─────────────────────────────────────────────────────
def test_more_folds_out_always_remove_and_adds_and_removes_phrases(built):
    page, app = built()
    d = page.dictionary
    assert d.remove_card.isHidden() and d.more_button.count_label.text() == "1 phrase"
    d.more_button.click()
    assert not d.remove_card.isHidden() and d.more_button.accessibleDescription() == "Expanded"
    d.phrase_input.setText("subtitles by the community")
    d.add_phrase_button.click()
    assert writes(app) == [("set_setting", "cleanup_custom_hallucinations",
                            ["thanks for watching", "subtitles by the community"])]
    assert d.more_button.count_label.text() == "2 phrases" and len(d.remove_rows) == 2
    assert d.phrase_input.text() == "" and d.remove_error.isHidden()
    d.phrase_input.setText("Thanks  for watching!")                # a repeat (ignoring case and punctuation)
    d.add_phrase_button.click()
    assert len(writes(app)) == 1 and not d.remove_error.isHidden() and d.remove_error.text() == "Already there."
    d.remove_rows[0].remove_button.click()
    assert writes(app)[-1] == ("set_setting", "cleanup_custom_hallucinations", ["subtitles by the community"])
    assert len(d.remove_rows) == 1 and d.more_button.count_label.text() == "1 phrase"
    d.more_button.click()
    assert d.remove_card.isHidden()


# ── the cloud toggle ─────────────────────────────────────────────────────────
def test_the_cloud_toggle_writes_the_setting(built):
    page, app = built()
    d = page.dictionary
    assert d.cloud_toggle.isChecked() and d.cloud_toggle.isEnabled()
    assert d.cloud_toggle.accessibleName() == "Use my words with cloud speech"
    assert d.cloud_row.hint_label.text() == ""
    d.cloud_toggle.click()
    assert writes(app) == [("set_setting", "vocabulary_share_with_cloud", False)]
    d.cloud_toggle.click()
    assert writes(app)[-1] == ("set_setting", "vocabulary_share_with_cloud", True)
    assert len(writes(app)) == 2


def test_the_cloud_toggle_is_off_and_disabled_in_privacy_mode(built):
    page, app = built(privacy_mode=True)
    d = page.dictionary
    assert not d.cloud_toggle.isEnabled() and not d.cloud_toggle.isChecked()
    assert d.cloud_row.hint_label.text() == "Off in Privacy mode"
    # A stored "share" value does not switch it on while Privacy mode is on.
    app.cfg["vocabulary_share_with_cloud"] = True
    page.on_setting_changed("vocabulary_share_with_cloud")
    assert not d.cloud_toggle.isChecked()
    app.cfg["privacy_mode"] = False
    page.on_setting_changed("privacy_mode")
    assert d.cloud_toggle.isEnabled() and d.cloud_toggle.isChecked() and d.cloud_row.hint_label.text() == ""
    assert writes(app) == []


def test_the_cloud_words_toggle_lives_only_in_the_dictionary_section(built):
    """One home for the setting: not a second copy in Privacy that could disagree in Privacy mode."""
    page, app = built()
    page.ensure_built()
    assert not hasattr(page, "t_words") and not hasattr(page, "row_words")
    name = "Use my words with cloud speech"
    rows = [w for w in page.findChildren(QWidget) if w.accessibleName() == name or
            (isinstance(w, QLabel) and w.text() == name)]
    assert rows, "the Dictionary's own toggle row is missing"
    d = page.dictionary
    assert all(d.isAncestorOf(w) for w in rows), "a copy of the toggle lives outside the Dictionary section"
    d.cloud_toggle.click()
    assert writes(app) == [("set_setting", "vocabulary_share_with_cloud", False)] or         writes(app) == [("set_settings", {"vocabulary_share_with_cloud": False})]


# ── config changes from elsewhere ────────────────────────────────────────────
def test_changes_from_elsewhere_redraw_the_embedded_lists(built):
    page, app = built()
    d = page.dictionary
    app.cfg["vocabulary"] = ["Aram", "Anna"]
    page.on_setting_changed("vocabulary")
    assert [c.word for c in d.chips] == ["Aram", "Anna"] and d.words_count.text() == "2 words"
    app.cfg["cleanup_replacements"] = [{"from": "a", "to": "b"}, {"from": "c", "to": "d"}]
    page.on_setting_changed("cleanup_replacements")
    assert len(d.replace_rows) == 2 and d.replace_count.text() == "2"
    app.cfg["cleanup_custom_hallucinations"] = ["one", "two", "three"]
    page.on_setting_changed("cleanup_custom_hallucinations")
    assert len(d.remove_rows) == 3 and d.more_button.count_label.text() == "3 phrases"
    app.cfg["vocabulary_share_with_cloud"] = False
    page.on_setting_changed("vocabulary_share_with_cloud")
    assert not d.cloud_toggle.isChecked() and d.cloud_toggle.isEnabled()
    app.cfg["privacy_mode"] = True
    page.on_setting_changed("privacy_mode")
    assert not d.cloud_toggle.isEnabled() and d.cloud_row.hint_label.text() == "Off in Privacy mode"
    app.cfg["privacy_mode"] = False
    app.cfg["vocabulary_share_with_cloud"] = True
    page.on_setting_changed("privacy_mode")
    assert d.cloud_toggle.isEnabled() and d.cloud_toggle.isChecked() and d.cloud_row.hint_label.text() == ""
    assert writes(app) == []                                      # nothing here writes back


def test_initial_prompt_changes_rebuild_nothing(built):
    page, app = built()
    d = page.dictionary
    chips, rows, phrase_rows = list(d.chips), list(d.replace_rows), list(d.remove_rows)
    app.cfg["initial_prompt"] = "Glossary: something else."
    page.on_setting_changed("initial_prompt")
    assert d.chips == chips and d.replace_rows == rows and d.remove_rows == phrase_rows


@pytest.mark.parametrize("key", ["hotkey", "backend", "save_history", "app_theme", "unrelated_key"])
def test_unrelated_keys_do_not_touch_the_dictionary(built, key):
    page, app = built()
    d = page.dictionary
    app.cfg["vocabulary"] = ["Zed"]                               # changed, but nobody said so
    app.cfg["cleanup_replacements"] = []
    chips, rows = list(d.chips), list(d.replace_rows)
    page.on_setting_changed(key)
    assert d.chips == chips and d.replace_rows == rows
    assert [c.word for c in d.chips] == WORDS
    page.on_setting_changed("vocabulary")                         # the right key does
    assert [c.word for c in d.chips] == ["Zed"]


def test_a_change_before_the_dictionary_is_built_is_just_picked_up_later(make):
    page, app = make(vocabulary=["Aram"])
    assert page.dictionary is None
    app.cfg["vocabulary"] = ["Aram", "Anna"]
    page.on_setting_changed("vocabulary")                         # no dictionary yet: no error
    page.ensure_built("dictionary")
    assert [c.word for c in page.dictionary.chips] == ["Aram", "Anna"]


# ── on_open: the proof sentence ──────────────────────────────────────────────
def test_on_open_reloads_the_lists_and_the_proof(built, quiet):
    page, app = built(vocabulary=["Mariam"], initial_prompt="Glossary: Mariam.")
    d = page.dictionary
    assert d.proof_row.isHidden()                                 # nothing in the history yet
    before = len(quiet.calls)
    quiet.sentence = "Ask Mariam about the invoice."
    app.cfg["vocabulary"] = ["Mariam", "Anna"]                    # and the list changed while away
    page.on_open()
    assert len(quiet.calls) > before and quiet.calls[-1] == ["Mariam", "Anna"]
    assert [c.word for c in d.chips] == ["Mariam", "Anna"]
    assert not d.proof_row.isHidden()
    assert d.proof_label.accessibleName() == "Ask Mariam about the invoice."
    assert "Mariam" in d.proof_label.text()
    # The sentence goes when its word goes.
    app.cfg["vocabulary"] = ["Anna"]
    page.on_setting_changed("vocabulary")
    assert d.proof_row.isHidden()


def test_on_open_without_words_loads_no_proof(built, quiet):
    page, _app = built(vocabulary=[], initial_prompt="")
    calls = len(quiet.calls)
    quiet.sentence = "Something"
    page.on_open()
    assert len(quiet.calls) == calls and page.dictionary.proof_row.isHidden()


def test_on_open_before_the_dictionary_exists_does_not_fail(make):
    page, _app = make()
    assert page.dictionary is None
    page.on_open()
    page.on_open(section="speech")
    assert page.dictionary is None or page.is_built("dictionary")


def test_a_dictionary_built_by_a_section_request_shows_the_proof(make, quiet):
    quiet.sentence = "Mariam is here."
    page, _app = make(show=True, vocabulary=["Mariam"])
    page.on_open(section="dictionary")
    assert not page.dictionary.proof_row.isHidden()
    assert page.dictionary.proof_label.accessibleName() == "Mariam is here."


# ── routing through the shell ────────────────────────────────────────────────
def test_the_old_dictionary_route_opens_the_settings_section(env):
    from ui.pages.settings import SettingsPage
    win, app = env.build("free", vocabulary=list(WORDS))
    win.show()
    page = win.open("dictionary")
    _spin(10)
    assert isinstance(page, SettingsPage) and win.current_route() == "settings"
    assert page.opts == {"section": "dictionary"}
    assert page.current_section() == "dictionary" and page.dictionary is not None
    assert [c.word for c in page.dictionary.chips] == WORDS
    bar = page.scroll.verticalScrollBar()
    assert _wait(lambda: page.is_built("updates"), 30)
    assert _wait(lambda: bar.value() == page.section_widget("dictionary").y(), 10),         (bar.value(), page.section_widget("dictionary").y(), bar.maximum())
    assert bar.value() > 0
    # The shell's config hook reaches the embedded page.
    app.cfg["vocabulary"] = ["Zed"]
    win.on_setting_changed("vocabulary")
    assert [c.word for c in page.dictionary.chips] == ["Zed"]
    win.open("settings", section="general")
    _spin(5)
    assert page.current_section() == "general"
    win.open("settings", section="words")
    _spin(5)
    assert page.current_section() == "dictionary"


def test_the_sidebar_has_no_dictionary_entry(env):
    win, _app = env.build("free")
    assert "dictionary" not in win.sidebar.items
    assert set(win.sidebar.items) == {"home", "dictation", "smart_actions", "meetings", "live_assistant",
                                      "audio_to_text", "settings"}


# ── a Dictionary that cannot be built ────────────────────────────────────────
def test_a_failing_dictionary_build_leaves_the_rest_of_settings_working(make, monkeypatch):
    import ui.pages.dictionary as dct

    class Broken(QWidget):
        def __init__(self, *a, **k):
            raise RuntimeError("no dictionary today")
    monkeypatch.setattr(dct, "DictionaryPage", Broken)
    page, app = make(show=True)
    page.ensure_built()                                           # no exception out of the build
    _spin()
    assert page.dictionary is None
    assert all(page.is_built(s) for s in LAZY)                    # privacy and updates still built
    assert page.section_widget("privacy") is not None and page.section_widget("updates") is not None
    sec = page.section_widget("dictionary")
    assert sec is not None and sec.accessibleName() == "Dictionary"
    texts = [lb.text() for lb in sec.findChildren(QLabel)]
    assert "Dictionary" in texts
    assert "The dictionary couldn't open. Restart Gusan and try again." in texts
    # The hooks that forward to it do nothing, quietly.
    page.on_open()
    page.on_open(section="dictionary")
    for key in DICT_KEYS + ("hotkey",):
        page.on_setting_changed(key)
    assert page.current_section() == "dictionary"
    page.nav.items["privacy"].click()
    _spin()
    assert page.current_section() == "privacy"
    assert writes(app) == []


def test_a_dictionary_that_fails_late_leaves_nothing_in_its_section(make, monkeypatch):
    """The page raises AFTER its widget exists: it must not stay behind, hidden under the section title."""
    import ui.pages.dictionary as dct
    real = dct.DictionaryPage

    class FailsLate(real):
        def __init__(self, *a, **k):
            super().__init__(*a, **k)
            raise RuntimeError("late failure")
    monkeypatch.setattr(dct, "DictionaryPage", FailsLate)
    page, _app = make(show=True)
    page.ensure_built()
    _spin()
    sec = page.section_widget("dictionary")
    assert page.dictionary is None
    assert sec.findChildren(real) == []
    assert "The dictionary couldn't open. Restart Gusan and try again." in [lb.text() for lb in sec.findChildren(QLabel)]


def test_a_failing_build_through_a_section_request_still_reaches_privacy(make, monkeypatch):
    import ui.pages.dictionary as dct
    monkeypatch.setattr(dct, "DictionaryPage", lambda *a, **k: 1 / 0)
    page, _app = make(show=True)
    page.show_section("privacy")
    _spin(5)
    assert page.dictionary is None and page.is_built("dictionary") and page.is_built("privacy")
    assert page.current_section() == "privacy"
